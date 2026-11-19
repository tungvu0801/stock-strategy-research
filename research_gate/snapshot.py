"""research_gate/snapshot.py - the lowest layer of the admission gate: the refusal exceptions and reason codes, the canonical fingerprint
function (and the fingerprint of an MD-B contract double), the strict loader that rebuilds an MD-A dataset from its canonical bytes plus its
side channel (_Snapshot), the query-state hash and the structural checks verify() relies on, the source re-hash, and the deep-copied snapshot
of a contract double. Nothing here reads a file except the source re-hash (inside the read guard, under the admitted source root only).

Determinism: every hash is sha256 of canonical JSON (sorted keys, no NaN); nothing depends on PYTHONHASHSEED, set order or id()."""
from __future__ import annotations

import copy
import dataclasses
import gzip
import hashlib
import json
from datetime import datetime
from pathlib import Path
from types import MappingProxyType

from historical_data import canonical as CN
from historical_data import schema as HSC
from historical_data import status as HS
from historical_data.adapters import ms1_evidence as MS1
from historical_data.provider import _TableProvider
from md_eval import contract as MC
from md_eval.double import ContractDouble

# ---- reason codes (Part A 2.6) ----------------------------------------------------------------------------------------------------------
NOT_A_REGISTERED_PROVIDER = "NOT_A_REGISTERED_PROVIDER"
CONTRACT_MISMATCH = "CONTRACT_MISMATCH"
CONTENT_HASH_STALE = "CONTENT_HASH_STALE"
SNAPSHOT_ROUNDTRIP_FAILED = "SNAPSHOT_ROUNDTRIP_FAILED"
RECORD_INVALID = "RECORD_INVALID"
DATASET_BLOCKED = "DATASET_BLOCKED"
SYNTHETIC_NOT_PINNED = "SYNTHETIC_NOT_PINNED"
LICENCE_EVIDENCE_UNDEFINED = "LICENCE_EVIDENCE_UNDEFINED"
NOT_EVALUATION = "NOT_EVALUATION"
DOUBLE_HISTORICAL_NOT_ADMITTED = "DOUBLE_HISTORICAL_NOT_ADMITTED"
FILE_READ_NOT_ALLOWED = "FILE_READ_NOT_ALLOWED"
NOT_ADMITTED = "NOT_ADMITTED"
NOT_ADMITTED_VIEW = "NOT_ADMITTED_VIEW"
ADMISSION_MISMATCH = "ADMISSION_MISMATCH"
SNAPSHOT_CHANGED = "SNAPSHOT_CHANGED"
SOURCE_CHANGED_DURING_RUN = "SOURCE_CHANGED_DURING_RUN"
SOURCE_UNREADABLE = "SOURCE_UNREADABLE"
BRIDGE_UNMAPPABLE = "BRIDGE_UNMAPPABLE"
ALLOWLIST_GUARD_FAILED = "ALLOWLIST_GUARD_FAILED"
OFFLINE_VIOLATION = "OFFLINE_VIOLATION"
CALENDAR_WARMUP_FAILED = "CALENDAR_WARMUP_FAILED"
GUARD_ROOT_NOT_ALLOWED = "GUARD_ROOT_NOT_ALLOWED"
REASON_CODES = (NOT_A_REGISTERED_PROVIDER, CONTRACT_MISMATCH, CONTENT_HASH_STALE, SNAPSHOT_ROUNDTRIP_FAILED, RECORD_INVALID, DATASET_BLOCKED,
                SYNTHETIC_NOT_PINNED, LICENCE_EVIDENCE_UNDEFINED, NOT_EVALUATION, DOUBLE_HISTORICAL_NOT_ADMITTED, FILE_READ_NOT_ALLOWED, NOT_ADMITTED,
                NOT_ADMITTED_VIEW, ADMISSION_MISMATCH, SNAPSHOT_CHANGED, SOURCE_CHANGED_DURING_RUN, SOURCE_UNREADABLE, BRIDGE_UNMAPPABLE,
                ALLOWLIST_GUARD_FAILED, OFFLINE_VIOLATION, CALENDAR_WARMUP_FAILED, GUARD_ROOT_NOT_ALLOWED)
# sub-reasons
SIDE_CHANNEL_MISMATCH, COUNTS_MISMATCH, CAP_EXCEEDED = "SIDE_CHANNEL_MISMATCH", "COUNTS_MISMATCH", "CAP_EXCEEDED"
PREFIX_NOT_SYNTHETIC, SYMBOL_NOT_INVENTED = "PREFIX_NOT_SYNTHETIC", "SYMBOL_NOT_INVENTED"
GUARD_NESTED, ROOT_MISSING, ROOT_INSIDE_REPOSITORY, ROOT_NOT_EMPTY = "GUARD_NESTED", "ROOT_MISSING", "ROOT_INSIDE_REPOSITORY", "ROOT_NOT_EMPTY"
INTEGRITY_ONLY_BAR = "INTEGRITY_ONLY_BAR"


class AdmissionRefused(RuntimeError):
    """A refusal of the admission gate. `code` is one reason code; `reasons` a tuple of codes; `decision` the acceptance decision when the
    refusal came after it was computed; `fingerprint` only on SYNTHETIC_NOT_PINNED (a sha256, never data). The message carries codes only."""

    def __init__(self, code, reasons=(), *, decision=None, fingerprint=None):
        self._code = str(code)
        self._reasons = tuple(str(r) for r in reasons)
        self._decision = json.loads(json.dumps(decision)) if decision is not None else None
        self._fingerprint = fingerprint if code == SYNTHETIC_NOT_PINNED else None
        tail = f" [{', '.join(self._reasons)}]" if self._reasons else ""
        fp = f" fingerprint={self._fingerprint}" if self._fingerprint else ""
        super().__init__(f"{self._code}{tail}{fp}")

    @property
    def code(self) -> str:
        return self._code

    @property
    def reasons(self) -> tuple:
        return self._reasons

    @property
    def decision(self):
        return json.loads(json.dumps(self._decision)) if self._decision is not None else None

    @property
    def fingerprint(self):
        return self._fingerprint


class NotAdmitted(AdmissionRefused):
    """NOT_ADMITTED, NOT_ADMITTED_VIEW and ADMISSION_MISMATCH: an object the gate did not issue, or one that no longer matches its issue."""


class FingerprintError(ValueError):
    pass


# ---- the canonical fingerprint form (Part A 6.2) ------------------------------------------------------------------------------------------
def canonical_value(x):
    """The JSON-native form of `x`, first matching rule wins: None / bool / str as-is; int; a finite float; an MD-B sentinel -> its name; a
    Missing (MD-A's or MD-B's) -> {status, reason}; a dataclass -> {"__type__": module.qualname, fields...}; tuple / list -> list; dict /
    MappingProxyType -> dict with string keys only. Anything else (set, datetime, a non-finite float, ...) raises FingerprintError."""
    if x is None or isinstance(x, (bool, str)):
        return x
    if isinstance(x, int):
        return x
    if isinstance(x, float):
        if x != x or x in (float("inf"), float("-inf")):
            raise FingerprintError("a non-finite float")
        return x
    if type(x) is MC._Sentinel:
        return x.name
    if type(x) in (HS.Missing, MC.Missing):
        return {"status": x.status, "reason": x.reason}
    if dataclasses.is_dataclass(x) and not isinstance(x, type):
        out = {"__type__": f"{type(x).__module__}.{type(x).__qualname__}"}
        for f in dataclasses.fields(x):
            out[f.name] = canonical_value(getattr(x, f.name))
        return out
    if isinstance(x, (tuple, list)):
        return [canonical_value(v) for v in x]
    if isinstance(x, (dict, MappingProxyType)):
        out = {}
        for k, v in x.items():
            if not isinstance(k, str):
                raise FingerprintError("a mapping key that is not a string")
            out[k] = canonical_value(v)
        return out
    raise FingerprintError(f"a value of type {type(x).__name__} has no canonical form")


def canonical_bytes(value) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False, ensure_ascii=False).encode("utf-8")


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def double_fingerprint_document(d) -> dict:
    """The private tables of an MD-B contract double, its manifest and its two knobs (the derived positions and the call recorders excluded)."""
    return {"kind_of_object": "MDB_CONTRACT_DOUBLE", "fingerprint_version": 1, "sessions": d._sessions, "instruments": d._instruments, "bars": d._bars,
            "events": d._events, "membership": d._membership, "scheduled": d._scheduled, "gaps": d._gaps, "manifest": d._manifest,
            "knobs": {"leak_known_at": d.leak_known_at, "view_holds_provider": d.view_holds_provider}}


def double_fingerprint(d) -> str:
    return sha256_hex(canonical_bytes(canonical_value(double_fingerprint_document(d))))


# ---- the MD-A snapshot (Part A 3.1, 3.2) ---------------------------------------------------------------------------------------------------
SNAPSHOT_ATTRS = frozenset(("_dropped", "_instruments", "_inst", "_bars", "_events", "_changes", "_scheduled", "_removed", "_notes", "_by_inst", "_at",
                            "_gaps", "_manifest"))
DOUBLE_ATTRS = frozenset(("_sessions", "_pos", "_instruments", "_bars", "_events", "_membership", "_scheduled", "_gaps", "_manifest", "leak_known_at",
                          "view_holds_provider", "calls", "exec_calls"))


class _Snapshot(_TableProvider):
    """An MD-A table provider rebuilt by the gate from canonical bytes and a side channel. The query indexes are frozen after construction
    (mapping proxies of tuples): an in-place change of a list is impossible; a rebinding is detected by verify (query-state hash, vars())."""

    def __init__(self, **kw):
        super().__init__(**kw)
        self._by_inst = MappingProxyType({k: tuple(v) for k, v in self._by_inst.items()})
        self._at = MappingProxyType({k: tuple(v) for k, v in self._at.items()})
        self._inst = MappingProxyType(dict(self._inst))


def side_channel_of(p) -> dict:
    """What the canonical form does not carry: the adapter notes and the dropped symbols (class-level reads, never instance attributes)."""
    return {"adapter_notes": [json.loads(json.dumps(n, sort_keys=True)) for n in _TableProvider.adapter_notes(p)],
            "dropped_symbols": list(_TableProvider.dropped_symbols(p))}


def side_channel_sha256(side: dict) -> str:
    return CN.sha256_bytes(CN.dumps(side))


def _is_missing_doc(v) -> bool:
    return isinstance(v, dict) and set(v) == {"status", "reason"}


def _missing(v):
    return HS.Missing(v["status"], v["reason"])


def _or_missing(v, conv=lambda x: x):
    return _missing(v) if _is_missing_doc(v) else conv(v)


def _ts(v) -> datetime:
    if not isinstance(v, str) or not v.endswith("Z"):
        raise ValueError("a timestamp is ISO-8601 UTC ending in Z")
    t = datetime.fromisoformat(v[:-1] + "+00:00")
    if t.tzinfo is None:
        raise ValueError("a naive timestamp")
    return t


def _keys(d, want, name):
    if not isinstance(d, dict) or set(d) != set(want):
        raise ValueError(f"{name}: unexpected keys")
    return d


def _licence(d):
    if not isinstance(d, dict):
        raise ValueError("licence")
    extra = {k: d[k] for k in ("licence_file_sha256", "confirmed_by", "confirmed_on") if k in d}
    return HSC.Licence(licence_ref=_or_missing(d["licence_ref"]), personal_research_use=d["personal_research_use"], redistribution=d["redistribution"],
                       **extra)


_MANIFEST_KEYS = ("dataset_id", "contract_version", "contract_sha256", "contract_base_sha256", "kind", "provider", "adapter", "adapter_version",
                  "source_files", "start_session", "end_session", "instrument_count", "price_views_available", "volume_feed", "price_source",
                  "membership_basis", "events_completeness", "adjustment_as_of", "retrieved_at", "licence", "data_exposure")


def _manifest_fields(d) -> dict:
    _keys(d, _MANIFEST_KEYS, "manifest")
    return dict(dataset_id=d["dataset_id"], kind=d["kind"], provider=d["provider"], adapter=d["adapter"], adapter_version=d["adapter_version"],
                source_files=tuple(HSC.SourceFile(**_keys(x, ("path", "file_sha256", "body_sha256"), "source_file")) for x in d["source_files"]),
                price_views_available=tuple(d["price_views_available"]), volume_feed=d["volume_feed"], price_source=d["price_source"],
                membership_basis=d["membership_basis"], events_completeness=d["events_completeness"], adjustment_as_of=_or_missing(d["adjustment_as_of"]),
                retrieved_at=_or_missing(d["retrieved_at"], _ts), licence=_licence(d["licence"]),
                data_exposure=_or_missing(d["data_exposure"], lambda x: tuple(dict(e) for e in x)), contract_version=d["contract_version"],
                contract_sha256=d["contract_sha256"], contract_base_sha256=d["contract_base_sha256"])


def _instrument(d):
    _keys(d, ("instrument_id", "symbol_history", "symbol_history_status", "asset_type", "listing", "sector", "issuer_id"), "instrument")
    spans = tuple(HSC.SymbolSpan(**_keys(x, ("symbol", "valid_from_session", "valid_to_session"), "symbol_span")) for x in d["symbol_history"])
    listing = _or_missing(d["listing"], lambda xs: tuple(HSC.ListingSpan(exchange=x["exchange"], from_session=x["from"], to_session=x["to"])
                                                         for x in (_keys(y, ("exchange", "from", "to"), "listing") for y in xs)))
    return HSC.Instrument(instrument_id=d["instrument_id"], symbol_history=spans, symbol_history_status=d["symbol_history_status"],
                          asset_type=_or_missing(d["asset_type"]), listing=listing, sector=_or_missing(d["sector"]), issuer_id=_or_missing(d["issuer_id"]))


_BAR_KEYS = ("instrument_id", "session", "raw_open", "raw_high", "raw_low", "raw_close", "raw_volume", "split_factor", "total_return_factor",
             "volume_feed", "price_source", "known_at", "source_ref")


def _bar(d):
    _keys(d, _BAR_KEYS, "bar")
    ref = _or_missing(d["source_ref"], lambda x: HSC.SourceRef(**_keys(x, ("file_sha256", "body_sha256", "index"), "source_ref")))
    return HSC.DailyBar(instrument_id=d["instrument_id"], session=d["session"], raw_open=_or_missing(d["raw_open"]), raw_high=_or_missing(d["raw_high"]),
                        raw_low=_or_missing(d["raw_low"]), raw_close=d["raw_close"], raw_volume=d["raw_volume"], split_factor=_or_missing(d["split_factor"]),
                        total_return_factor=_or_missing(d["total_return_factor"]), volume_feed=d["volume_feed"], price_source=d["price_source"],
                        known_at=_ts(d["known_at"]), source_ref=ref)


_EVENT_KEYS = ("event_id", "instrument_id", "type", "effective_session", "known_at", "known_at_status", "record_session", "pay_session",
               "ratio", "cash_amount", "cash_in_lieu_per_share", "child_instrument_id", "child_ratio", "cost_basis_allocation", "acquirer_instrument_id",
               "stock_ratio", "last_trade_session", "last_raw_price", "delisting_reason", "delisting_return")


def _event(d):
    _keys(d, _EVENT_KEYS, "event")
    kw = {k: _or_missing(d[k]) for k in _EVENT_KEYS[6:]}
    e = HSC.CorporateEvent(event_id=d["event_id"], instrument_id=d["instrument_id"], type=d["type"], effective_session=d["effective_session"],
                           known_at=_or_missing(d["known_at"], _ts), **kw)
    if e.known_at_status != d["known_at_status"]:
        raise ValueError("known_at_status differs from the recomputed property")
    return e


def _change(d):
    _keys(d, ("universe_id", "instrument_id", "session", "action", "known_at"), "membership")
    return HSC.MembershipChange(d["universe_id"], d["instrument_id"], d["session"], d["action"], _or_missing(d["known_at"], _ts))


def _scheduled(d):
    if _is_missing_doc(d):
        return _missing(d)
    return [HSC.ScheduledEvent(event_id=x["event_id"], instrument_id=x["instrument_id"], event_session=x["event_session"], timing=x["timing"],
                               known_at=_ts(x["known_at"]), kind=x["kind"])
            for x in (_keys(y, ("event_id", "instrument_id", "event_session", "timing", "known_at", "kind"), "scheduled_event") for y in d)]


def load_snapshot(data: bytes, side: dict) -> _Snapshot:
    """The strict loader: canonical bytes + side channel -> _Snapshot. Decoding is positional (a {status, reason} object becomes Missing only
    in a field that may be Missing); removed rows are re-derived from the REMOVED_INVALID gaps. Any load or type error raises."""
    doc = json.loads(data.decode("utf-8"))
    _keys(doc, ("manifest", "instruments", "bars", "gaps", "events", "membership", "scheduled_events"), "document")
    removed = sorted((g["instrument_id"], g["session"]) for g in (_keys(x, ("instrument_id", "session", "reason"), "gap") for x in doc["gaps"])
                     if g["reason"] == "REMOVED_INVALID")
    _keys(side, ("adapter_notes", "dropped_symbols"), "side_channel")
    return _Snapshot(manifest_fields=_manifest_fields(doc["manifest"]), instruments=[_instrument(x) for x in doc["instruments"]],
                     bars=[_bar(x) for x in doc["bars"]], events=[_event(x) for x in doc["events"]], membership=[_change(x) for x in doc["membership"]],
                     scheduled_events=_scheduled(doc["scheduled_events"]), removed=removed, notes=list(side["adapter_notes"]),
                     dropped_symbols=list(side["dropped_symbols"]))


def removed_in_notes(side: dict) -> list:
    """The union of the removed_invalid lists every adapter records in its notes (Part A F-5 cross-check)."""
    out = set()
    for n in side["adapter_notes"]:
        if isinstance(n, dict) and "removed_invalid" in n:
            out |= {tuple(x) for x in n["removed_invalid"]}
    return sorted(out)


def roundtrip_problems(snap: _Snapshot, data: bytes, sha: str, side: dict) -> list:
    """Why the rebuilt snapshot is not the dataset it was built from (an empty list: it is)."""
    out = []
    if CN.dumps(_TableProvider.canonical_document(snap)) != data:
        out.append("CANONICAL_BYTES")
    if _TableProvider.manifest(snap).content_sha256 != sha:
        out.append("CONTENT_SHA256")
    if side_channel_of(snap) != side:
        out.append("SIDE_CHANNEL")
    doc = json.loads(data.decode("utf-8"))["manifest"]
    m = _TableProvider.manifest(snap)
    if (HSC.jv(m.start_session), HSC.jv(m.end_session), m.instrument_count) != (doc["start_session"], doc["end_session"], doc["instrument_count"]):
        out.append("DERIVED_FIELDS")
    if sorted(snap._removed) != removed_in_notes(side):
        out.append("REMOVED_ROWS")
    return out


# ---- the query-state hash and the structural checks (Part A 3.3) -----------------------------------------------------------------------------
def _json(x):
    return x.to_json() if hasattr(x, "to_json") else x


def query_state_document(snap) -> dict:
    if type(snap) is _Snapshot:
        return {"manifest": snap._manifest.to_json(), "instruments": [i.to_json() for i in snap._instruments],
                "inst": [[iid, snap._inst[iid].to_json()] for iid in sorted(snap._inst)], "bars": [b.to_json() for b in snap._bars],
                "by_inst": [[iid, [b.to_json() for b in snap._by_inst[iid]]] for iid in sorted(snap._by_inst)],
                "at": [[[k[0], k[1]], [b.to_json() for b in snap._at[k]]] for k in sorted(snap._at)], "gaps": [g.to_json() for g in snap._gaps],
                "events": [e.to_json() for e in snap._events], "changes": [c.to_json() for c in snap._changes],
                "scheduled": _json(snap._scheduled) if HS.is_missing(snap._scheduled) else [s.to_json() for s in snap._scheduled],
                "removed": sorted(list(x) for x in snap._removed), "notes": list(snap._notes), "dropped": list(snap._dropped)}
    if type(snap) is ContractDouble:
        return dict(double_fingerprint_document(snap), pos=sorted(snap._pos.items()))
    raise TypeError("not a snapshot")


def query_state_sha256(snap) -> str:
    if type(snap) is _Snapshot:
        return CN.sha256_bytes(CN.dumps(query_state_document(snap)))
    return sha256_hex(canonical_bytes(canonical_value(query_state_document(snap))))


def structure_ok(snap) -> bool:
    """The exact type, the exact attribute set and (MD-A) the frozen index types: an added attribute or an instance-level method override fails."""
    if type(snap) is _Snapshot:
        return set(vars(snap)) == SNAPSHOT_ATTRS and type(snap._by_inst) is type(snap._at) is type(snap._inst) is MappingProxyType
    if type(snap) is ContractDouble:
        return set(vars(snap)) == DOUBLE_ATTRS
    return False


# ---- the source re-hash (Part A 3.4) ---------------------------------------------------------------------------------------------------------
def source_changes(root: Path, source_files) -> list:
    """The listed source files whose bytes or decompressed body no longer match the manifest (an unreadable one counts)."""
    out = []
    for path, file_sha, body_sha in source_files:
        try:
            p = MS1.inside(Path(root), path)
            with open(p, "rb") as f:
                stored = f.read()
            body = gzip.decompress(stored) if path.endswith(".gz") else stored
        except (OSError, EOFError, ValueError, MS1.SourceFormatError):
            out.append(path)
            continue
        if sha256_hex(stored) != file_sha or sha256_hex(body) != body_sha:
            out.append(path)
    return out


# ---- the contract-double snapshot (Part A 3.5) -----------------------------------------------------------------------------------------------
_DOUBLE_RECORD_TYPES = (MC.Instrument, MC.DailyBar, MC.CorporateEvent, MC.Gap, MC.ScheduledEvent)


def double_types_ok(d) -> bool:
    """Every record of the double's tables is exactly an md_eval.contract record (a subclass, an MD-A record or a look-alike is refused)."""
    if type(d._manifest) is not MC.DatasetManifest:
        return False
    if not all(type(i) is MC.Instrument for i in d._instruments.values()):
        return False
    if not all(type(b) is MC.DailyBar for v in d._bars.values() for b in v):
        return False
    if not all(type(e) is MC.CorporateEvent for e in d._events) or not all(type(g) is MC.Gap for g in d._gaps):
        return False
    if d._scheduled is not MC.EARNINGS_DATA_UNAVAILABLE:
        try:
            if not all(type(x) is MC.ScheduledEvent for x in d._scheduled):
                return False
        except TypeError:
            return False
    return True


def snapshot_double(d) -> ContractDouble:
    """A deep copy of the double's private tables through the public constructor; the sentinel memo is built fresh on every call (a shared memo
    would alias copies across admissions); the call recorders are forwarded (the caller's own lists)."""
    memo = {id(MC.NO_BAR): MC.NO_BAR, id(MC.EARNINGS_DATA_UNAVAILABLE): MC.EARNINGS_DATA_UNAVAILABLE}
    sessions, instruments, bars, manifest, events, membership, scheduled, gaps = copy.deepcopy(
        (d._sessions, tuple(d._instruments.values()), [b for v in d._bars.values() for b in v], d._manifest, d._events, d._membership, d._scheduled,
         d._gaps), memo)
    snap = ContractDouble(sessions=sessions, instruments=instruments, bars=bars, manifest=manifest, events=events, membership=membership,
                          scheduled=scheduled, gaps=gaps, leak_known_at=d.leak_known_at, view_holds_provider=d.view_holds_provider)
    snap.calls = d.calls
    snap.exec_calls = d.exec_calls
    return snap
