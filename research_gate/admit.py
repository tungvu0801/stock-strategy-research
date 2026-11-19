"""research_gate/admit.py - the admission gate (I-1A 2.3-2.7 with Addendum A): Admission, AdmittedDataset, their tokens and issuance
registries; admit (an MD-A SyntheticProvider), admit_from_source (an MD-A adapter the gate itself builds from a source root), admit_double
(an MD-B contract double), admit_for_engine (md_eval.engine.run's dispatcher); verify; the calendar warm-up; the file-read guard; the read log.

Every admission and every verify runs under md_eval.isolation.offline() and the read guard, after the calendar warm-up:
    with offline() as attempts:
        warm-up (the calendar cross-check, the tzdata status, the zone - before the guard: they import modules and read files once)
        with read_guard(root) as guard: the steps
    a guard violation refuses FILE_READ_NOT_ALLOWED and an offline attempt OFFLINE_VIOLATION, even when the error was swallowed, and both take
    precedence over the code of the step that failed.

The read guard is ONE permanent audit hook (sys.addaudithook), installed once per process and inert unless a guard is active in the current
context. It confines file opens and directory listings during admission and verify only. It is NOT a complete security sandbox: deletes,
renames, mkdir and stat go through, a new thread does not inherit the guard, and in-process code can bypass it (residual risk RR-1).
Network and SQLite access are offline()'s job. Importing this module installs one audit hook (one sys.addaudithook event), sets the sentinel
sys._research_gate_audit_hook = (hook, guard state) and raises one research_gate.selftest audit event; a malformed sentinel, or a hook that
does not observe its self-test, fails the import (fail closed).

No HISTORICAL dataset can be admitted (ID-8): HISTORICAL_EVIDENCED is reserved and never issued; ADMITTED_AS_REACHABLE lists what can be."""
from __future__ import annotations

import collections
import contextlib
import contextvars
import copy
import fnmatch
import gzip
import hashlib
import importlib.metadata
import importlib.util
import json
import os
import sys
import weakref
import zlib
import zoneinfo
from pathlib import Path

import historical_data as HD
import md_eval
from historical_data import acceptance as AC
from historical_data import calendar as CAL
from historical_data import canonical as CN
from historical_data import expected_events as EE
from historical_data import schema as HSC
from historical_data import status as HS
from historical_data import validate as VA
from historical_data.adapters import ms1_evidence as MS1
from historical_data.adapters import sip_daily_v1 as SIP
from historical_data.adapters import synthetic as SY
from historical_data.provider import _TableProvider
from md_eval import contract as MC
from md_eval.double import ContractDouble
from md_eval.isolation import offline
from research_gate import bridge as BR
from research_gate import licence_evidence as LE
from research_gate import snapshot as SN
from research_gate import synthetic_allowlist as SAL

ADMISSION_VERSION = "research_gate_admission_v1"
HISTORICAL_EVIDENCED = "HISTORICAL_EVIDENCED"                     # reserved (ID-8): never constructed, never issued
ADMITTED_AS_REACHABLE = ("SYNTHETIC_PINNED", "SYNTHETIC_DOUBLE")
KIND_OF_OBJECT = {"SYNTHETIC": "MDA_SYNTHETIC_PROVIDER", MS1.ADAPTER_VERSION: "MDA_MS1_EVIDENCE_ARCHIVE", SIP.ADAPTER_VERSION: "MDA_SIP_FIXTURE_BUNDLE",
                  "DOUBLE": "MDB_CONTRACT_DOUBLE"}
SOURCE_ADAPTERS = (MS1.ADAPTER_VERSION, SIP.ADAPTER_VERSION)
_REPO_ROOT = Path(__file__).resolve().parents[1]
_TOUCHED = (copy, gzip, hashlib, importlib.metadata, json, weakref, zlib, zoneinfo, EE, HSC)   # imported before any guard: no import happens inside it

# ---- the read log (I1-G01): passive, no flag, no setter -----------------------------------------------------------------------------------
_READ_LOG = collections.deque(maxlen=100_000)


def read_log() -> tuple:
    """(phase, kind_of_object, dataset_id) for every successful ADMIT_READ / REVERIFY_READ / VERIFY_READ, oldest first."""
    return tuple(_READ_LOG)


# ---- the read guard: one permanent audit hook, inert outside a guard (Part A 2.5) ----------------------------------------------------------
_WRITE_FLAGS = os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_APPEND | os.O_TRUNC


def _tzdata_dirs() -> tuple:
    spec = importlib.util.find_spec("tzdata")
    if spec is None or not spec.submodule_search_locations:
        return ()
    return tuple(Path(p).resolve() for p in spec.submodule_search_locations)


_TZDATA_DIRS = _tzdata_dirs()


def _resolve(path) -> Path:
    return Path(os.fsdecode(os.fspath(path))).resolve()


def _under(p: Path, base: Path) -> bool:
    return p == base or p.is_relative_to(base)


def _is_write(mode, flags) -> bool:
    if mode is not None and not isinstance(mode, str):
        return True
    if isinstance(mode, str) and any(c in mode for c in "wax+"):
        return True
    return not isinstance(flags, int) or bool(flags & _WRITE_FLAGS)


class _GuardState:
    """One active guard: its root (resolved) or None, its mode, the violations it recorded, the resolved sys.path entries and tzdata dirs."""
    __slots__ = ("root", "allow_write", "violations", "_syspath", "_tz")

    def __init__(self, root, write_mode):
        self.root = root
        self.allow_write = write_mode
        self.violations = []
        paths = []
        for p in list(sys.path):
            if isinstance(p, str):
                try:
                    paths.append(Path(p).resolve())
                except (OSError, ValueError, RuntimeError):
                    continue
        self._syspath = tuple(paths)
        self._tz = _TZDATA_DIRS

    def _tz_ok(self, p: Path) -> bool:
        for d in self._tz:
            if _under(p, d):
                return True
            try:
                first = p.relative_to(d.parent).parts[0]
            except (ValueError, IndexError):
                continue
            if fnmatch.fnmatch(first.lower(), "tzdata-*.dist-info"):
                return True
        return False

    def allows(self, event, args) -> bool:
        if event == "open":
            path, mode, flags = args
            if path is None or isinstance(path, int):
                return False
            p = _resolve(path)
            if _is_write(mode, flags):
                return self.allow_write and self.root is not None and _under(p, self.root)
            if self.root is not None and _under(p, self.root):
                return True
            if p.suffix.lower() in (".py", ".pyc") and any(_under(p, s) for s in self._syspath):
                return True
            return self._tz_ok(p)
        if event in ("os.listdir", "os.scandir"):
            (path,) = args
            if isinstance(path, int):
                return False
            p = _resolve(path if path is not None else ".")
            if self.root is not None and _under(p, self.root):
                return True
            return any(_under(p, s) for s in self._syspath) or self._tz_ok(p)
        return False


def _make_hook():
    """The hook and its own guard state (a ContextVar), bound together in its closure; it reads the state only through that closure."""
    guard_var = contextvars.ContextVar("research_gate_read_guard", default=None)
    names = frozenset(("open", "os.listdir", "os.scandir", "research_gate.selftest"))
    seen = []

    def hook(event, args):
        if event not in names:
            return
        if event == "research_gate.selftest":
            seen.append((args[0] if args else None, guard_var.get()))
            return
        g = guard_var.get()
        if g is None:
            return                                                                   # inert: the arguments are never touched
        try:
            ok = g.allows(event, args)
        except Exception:                                                            # an argument shape the guard cannot read: refused
            ok = False
        if not ok:
            try:
                g.violations.append((event, repr(args[0])[:300] if args else ""))
            except Exception:
                pass
            raise PermissionError(f"research_gate read guard: {event} refused")
    hook.guard_var = guard_var
    hook.selftest_seen = seen
    return hook


def _sentinel_ok(s) -> bool:
    return (type(s) is tuple and len(s) == 2 and callable(s[0]) and type(s[1]) is contextvars.ContextVar and hasattr(s[0], "selftest_seen")
            and getattr(s[0], "guard_var", None) is s[1])


def _selftest(s) -> None:
    """The hook must observe a private event, and read the same guard state this module sets (otherwise the guard would be inert)."""
    token, probe = object(), object()
    t = _GUARD.set(probe)
    try:
        sys.audit("research_gate.selftest", token)
    finally:
        _GUARD.reset(t)
    if not any(a is token and b is probe for a, b in list(getattr(s[0], "selftest_seen", ()))):
        raise ImportError("research_gate: the read guard's audit hook did not observe its self-test (fail closed)")


_SENTINEL = getattr(sys, "_research_gate_audit_hook", None)
if _SENTINEL is None:
    _HOOK = _make_hook()
    sys.addaudithook(_HOOK)
    _GUARD = _HOOK.guard_var
    _selftest((_HOOK, _GUARD))
    sys._research_gate_audit_hook = (_HOOK, _GUARD)
else:
    if not _sentinel_ok(_SENTINEL):
        raise ImportError("research_gate: the audit-hook sentinel is malformed (fail closed)")
    _HOOK, _GUARD = _SENTINEL
    _selftest(_SENTINEL)


def _write_root(root) -> Path:
    if root is None:
        raise SN.AdmissionRefused(SN.GUARD_ROOT_NOT_ALLOWED, (SN.ROOT_MISSING,))
    try:
        r = Path(root).resolve(strict=True)
        ok = r.is_dir()
    except (OSError, RuntimeError, TypeError, ValueError):
        ok = False
    if not ok:
        raise SN.AdmissionRefused(SN.GUARD_ROOT_NOT_ALLOWED, (SN.ROOT_MISSING,))
    if _under(r, _REPO_ROOT):
        raise SN.AdmissionRefused(SN.GUARD_ROOT_NOT_ALLOWED, (SN.ROOT_INSIDE_REPOSITORY,))
    if next(r.iterdir(), None) is not None:
        raise SN.AdmissionRefused(SN.GUARD_ROOT_NOT_ALLOWED, (SN.ROOT_NOT_EMPTY,))
    return r


@contextlib.contextmanager
def read_guard(root=None, *, allow_write=False):
    """Confine file opens and listings to the import machinery's .py / .pyc reads under sys.path, the tzdata package and its dist-info, and
    (read-only) the resolved `root`. allow_write=True (test builders only) also allows writes, only under a root that exists, is empty and lies
    outside the repository; it is off again after exit. Inside an active guard only a read-only guard with root=None may nest (it narrows)."""
    if _GUARD.get() is not None and (allow_write or root is not None):
        raise SN.AdmissionRefused(SN.GUARD_ROOT_NOT_ALLOWED, (SN.GUARD_NESTED,))
    if allow_write:
        r = _write_root(root)
    else:
        r = Path(root).resolve(strict=True) if root is not None else None
    state = _GuardState(r, bool(allow_write))
    token = _GUARD.set(state)
    try:
        yield state
    finally:
        _GUARD.reset(token)


def _warm_calendar() -> None:
    """The calendar cross-check, the tzdata status and the zone cache, filled before the guard is entered (every admission and verify)."""
    try:
        ok = bool(CAL.tzdata_status()[0])
    except Exception:
        ok = False
    if not ok:
        raise SN.AdmissionRefused(SN.CALENDAR_WARMUP_FAILED, ("TZDATA_UNAVAILABLE",))
    try:
        CAL.crosscheck_cached()
        CAL.close_utc(CAL.SESSIONS[0])
    except Exception as exc:
        raise SN.AdmissionRefused(SN.CALENDAR_WARMUP_FAILED, (type(exc).__name__,)) from None


def _guarded(root, fn):
    """Run fn() under offline() and the read guard, after the warm-up; the guard's violations, then the offline attempts, take precedence over
    the code fn raised (kept as __context__)."""
    err, result, violations = None, None, []
    with offline() as attempts:
        try:
            _warm_calendar()
            with read_guard(root) as g:
                try:
                    result = fn()
                finally:
                    violations = list(g.violations)
        except Exception as exc:
            err = exc
    if violations:
        refusal = SN.AdmissionRefused(SN.FILE_READ_NOT_ALLOWED, (SN.FILE_READ_NOT_ALLOWED,))
    elif attempts:
        refusal = SN.AdmissionRefused(SN.OFFLINE_VIOLATION, (SN.OFFLINE_VIOLATION,))
    elif err is not None:
        raise err
    else:
        return result
    refusal.__context__ = err
    raise refusal


# ---- Admission, AdmittedDataset (Part A 2.8) -------------------------------------------------------------------------------------------------
_ADMISSION_TOKEN = object()
_DATASET_TOKEN = object()
ADM_REG: dict = {}
DS_REG: dict = {}
ADMISSION_FIELDS = ("admission_version", "dataset_id", "admitted_as", "kind_of_object", "content_sha256", "fingerprint", "contract_version",
                    "contract_sha256", "contract_base_sha256", "criteria_version", "record_sha256", "decision_json", "decision_sha256",
                    "side_channel_sha256", "allowlist_entry_id", "licence_evidence_sha256", "source_root_id", "source_files", "admission_sha256")


class Admission:
    """The gate's record of one admission. Built only by the gate (a module-private token); read-only; identified by identity, never equality."""
    __slots__ = ADMISSION_FIELDS + ("__weakref__",)

    def __init__(self, _token, **fields):
        if _token is not _ADMISSION_TOKEN:
            raise SN.NotAdmitted(SN.NOT_ADMITTED)
        for k in ADMISSION_FIELDS:
            object.__setattr__(self, k, fields[k])

    def __setattr__(self, name, v):
        raise AttributeError("an Admission is read-only")

    def __delattr__(self, name):
        raise AttributeError("an Admission is read-only")

    @property
    def decision(self):
        return json.loads(self.decision_json) if self.decision_json is not None else None


def _issued_admission(x) -> bool:
    return type(x) is Admission and BR._issued(ADM_REG, x)


def _issued_dataset(x) -> bool:
    return type(x) is AdmittedDataset and BR._issued(DS_REG, x)


class AdmittedDataset:
    """An admitted dataset: MD-B's provider protocol over the gate's own snapshot (an MD-A _Snapshot converted by the bridge, or a deep-copied
    contract double whose call recorders are the caller's). No public API returns the snapshot."""
    __slots__ = ("_admission", "_snapshot", "_root", "_flavour", "_qsha", "__weakref__")

    def __init__(self, _token, admission, snapshot, root, flavour, qsha):
        if _token is not _DATASET_TOKEN:
            raise SN.NotAdmitted(SN.NOT_ADMITTED)
        for k, v in (("_admission", admission), ("_snapshot", snapshot), ("_root", root), ("_flavour", flavour), ("_qsha", qsha)):
            object.__setattr__(self, k, v)

    def __setattr__(self, name, v):
        raise AttributeError("an AdmittedDataset is read-only")

    def __delattr__(self, name):
        raise AttributeError("an AdmittedDataset is read-only")

    @property
    def admission(self) -> Admission:
        return self._admission

    def manifest(self):
        if self._flavour == "MDB":
            return self._snapshot.manifest()
        return BR.manifest(_TableProvider.manifest(self._snapshot))

    def instruments(self) -> list:
        if self._flavour == "MDB":
            return self._snapshot.instruments()
        return BR.mda_instruments(self._snapshot)

    def bars(self, instrument_id, start_session, end_session) -> list:
        if self._flavour == "MDB":
            return self._snapshot.bars(instrument_id, start_session, end_session)
        return BR.mda_bars(self._snapshot, instrument_id, start_session, end_session)

    def gaps(self, instrument_id) -> list:
        if self._flavour == "MDB":
            return self._snapshot.gaps(instrument_id)
        return BR.mda_gaps(self._snapshot, instrument_id)

    def events(self, instrument_id=None, start_session=None, end_session=None) -> list:
        if self._flavour == "MDB":
            return self._snapshot.events(instrument_id, start_session, end_session)
        return BR.mda_events(self._snapshot, instrument_id, start_session, end_session)

    def membership(self, universe_id, session):
        if self._flavour == "MDB":
            return self._snapshot.membership(universe_id, session)
        return BR.mda_membership(self._snapshot, universe_id, session)

    def scheduled_events(self, instrument_id, start_session, end_session):
        if self._flavour == "MDB":
            return self._snapshot.scheduled_events(instrument_id, start_session, end_session)
        return BR.mda_scheduled(self._snapshot, instrument_id, start_session, end_session)

    def session_times(self, session):
        if self._flavour == "MDB":
            return self._snapshot.session_times(session)
        return BR.mda_session_times(self._snapshot, session)

    def sessions(self, start_session, end_session) -> list:
        if self._flavour == "MDB":
            return self._snapshot.sessions(start_session, end_session)
        return BR.mda_sessions(self._snapshot, start_session, end_session)

    def next_session(self, session) -> str:
        if self._flavour == "MDB":
            return self._snapshot.next_session(session)
        return BR.mda_next_session(self._snapshot, session)

    def as_of(self, decision_time_utc):
        if self._flavour == "MDB":
            return BR._issue_view(self._snapshot.as_of(decision_time_utc), self._admission.admission_sha256, "MDB", None)
        return BR.mda_as_of(self._snapshot, decision_time_utc, self._admission.admission_sha256)

    def execution_open(self, instrument_id, session, *, decision_session):
        if self._flavour == "MDB":
            return self._snapshot.execution_open(instrument_id, session, decision_session=decision_session)
        return BR.mda_execution_open(self._snapshot, instrument_id, session, decision_session=decision_session)


def _admission_sha(fields: dict) -> str:
    doc = {k: fields[k] for k in ADMISSION_FIELDS if k not in ("decision_json", "admission_sha256")}
    doc["source_files"] = [list(x) for x in doc["source_files"]]
    return CN.sha256_bytes(CN.dumps(doc))


def _issue(fields: dict, snap, root, flavour: str) -> AdmittedDataset:
    """The only place an Admission and an AdmittedDataset are built; both are registered by identity."""
    fields = dict(fields, admission_sha256=_admission_sha(fields))
    adm = Admission(_ADMISSION_TOKEN, **fields)
    BR._register(ADM_REG, adm)
    ds = AdmittedDataset(_DATASET_TOKEN, adm, snap, root, flavour, SN.query_state_sha256(snap))
    BR._register(DS_REG, ds)
    _READ_LOG.append(("ADMIT_READ", fields["kind_of_object"], fields["dataset_id"]))
    return ds


# ---- the steps (Part A 2.4, 2.7) --------------------------------------------------------------------------------------------------------
def _refuse(code, reasons=(), decision=None):
    raise SN.AdmissionRefused(code, reasons or (code,), decision=decision)


def _mda_contract_ok(m) -> bool:
    v, s, b = getattr(m, "contract_version", None), getattr(m, "contract_sha256", None), getattr(m, "contract_base_sha256", None)
    return (v in HD.SUPPORTED_CONTRACT_VERSIONS and v == md_eval.CONTRACT_V2 and s == HD.CONTRACT_SHA256 == md_eval.CONTRACT_V2_SHA256
            and b == HD.CONTRACT_BASE_SHA256 == md_eval.CONTRACT_V1_SHA256)


def _double_contract_ok(m) -> bool:
    v, s = getattr(m, "contract_version", None), getattr(m, "contract_sha256", None)
    base = md_eval.CONTRACT_BASE_OF.get(v) if isinstance(v, str) else None
    return md_eval.SUPPORTED_CONTRACTS.get(v) == s and s is not None and base is not None and base[1] == HD.CONTRACT_BASE_SHA256


def _check_record(record, sha: str) -> None:
    """Step 5 (MDA-FR-3): the record's verdict, its own sha and its content shas, recomputed."""
    v = record.get("verdict") if isinstance(record, dict) else None
    if v == HD.BLOCK:
        _refuse(SN.DATASET_BLOCKED)
    if v not in (HD.CLEAN, HD.WARN):
        _refuse(SN.RECORD_INVALID, (SN.RECORD_INVALID, "VERDICT"))
    try:
        again = CN.sha256_bytes(CN.dumps({k: x for k, x in record.items() if k != "record_sha256"}))
    except (TypeError, ValueError):
        again = None
    if again is None or again != record.get("record_sha256"):
        _refuse(SN.RECORD_INVALID, (SN.RECORD_INVALID, "RECORD_SHA256"))
    if not (record.get("content_sha256") == record.get("recomputed_content_sha256") == sha):
        _refuse(SN.RECORD_INVALID, (SN.RECORD_INVALID, "CONTENT_SHA256"))


def _record_and_decision(snap, sha: str):
    try:
        record = VA.validate(snap)
    except SN.AdmissionRefused:
        raise
    except Exception as exc:
        raise SN.AdmissionRefused(SN.RECORD_INVALID, (SN.RECORD_INVALID, type(exc).__name__)) from None
    _check_record(record, sha)
    decision = AC.decide(record, _TableProvider.manifest(snap), instruments=_TableProvider.instruments(snap),
                         earnings_unavailable=(_TableProvider.all_scheduled_events(snap) == HS.EARNINGS_DATA_UNAVAILABLE))
    return record, decision


def _lookup(kind: str, fingerprint: str):
    hits = [e for e in SAL.ENTRIES if e.get("kind_of_object") == kind and e.get("fingerprint") == fingerprint]
    return hits[0] if len(hits) == 1 else None


def _symbol_of(iid: str) -> str:
    parts = iid.split(":")
    return parts[1] if len(parts) >= 3 else ""


def _guard_problem(entry, kind, counts, ids, side_sha):
    """The allow-list entry's guards (Part A 6.1): the side channel, the counts, the caps, the synthetic prefixes, the invented symbols."""
    if entry.get("side_channel_sha256") != side_sha:
        return SN.SIDE_CHANNEL_MISMATCH
    span = entry.get("session_span")
    if (entry.get("instrument_count"), entry.get("bar_count"), tuple(span) if span is not None else None) != counts:
        return SN.COUNTS_MISMATCH
    caps = SAL.CAPS.get(kind)
    if caps is None or counts[0] > caps["max_instruments"] or counts[1] > caps["max_bars"]:
        return SN.CAP_EXCEEDED
    if not all(i.startswith(tuple(caps["id_prefixes"])) for i in ids):
        return SN.PREFIX_NOT_SYNTHETIC
    if "invented_symbols" in caps and not all(_symbol_of(i) in caps["invented_symbols"] for i in ids):
        return SN.SYMBOL_NOT_INVENTED
    return None


def _mda_counts(snap):
    m = _TableProvider.manifest(snap)
    return len(snap._instruments), len(snap._bars), (HSC.jv(m.start_session), HSC.jv(m.end_session))


def _mda_ids(snap) -> list:
    return [i.instrument_id for i in snap._instruments]


def _kind_for(m, root) -> str:
    return KIND_OF_OBJECT["SYNTHETIC"] if root is None else KIND_OF_OBJECT.get(m.adapter_version, "MDA_UNKNOWN_SOURCE")


def _mda_fields(m, kind, sha, record, decision, side_sha, entry, root) -> dict:
    files = tuple((s.path, s.file_sha256, s.body_sha256) for s in m.source_files) if root is not None else ()
    decision_json = CN.dumps(decision)
    return dict(admission_version=ADMISSION_VERSION, dataset_id=m.dataset_id, admitted_as="SYNTHETIC_PINNED", kind_of_object=kind, content_sha256=sha,
                fingerprint=sha, contract_version=m.contract_version, contract_sha256=m.contract_sha256, contract_base_sha256=m.contract_base_sha256,
                criteria_version=AC.CRITERIA_VERSION, record_sha256=record["record_sha256"], decision_json=decision_json,
                decision_sha256=CN.sha256_bytes(decision_json), side_channel_sha256=side_sha,
                allowlist_entry_id=entry["allowlist_entry_id"] if entry is not None else None, licence_evidence_sha256=None,
                source_root_id=CN.sha256_bytes(CN.dumps(sorted(list(x) for x in files))) if root is not None else None, source_files=files)


def _canonical(p):
    try:
        data = CN.dumps(_TableProvider.canonical_document(p))
    except (TypeError, ValueError, AttributeError, KeyError) as exc:
        raise SN.AdmissionRefused(SN.CONTENT_HASH_STALE, (SN.CONTENT_HASH_STALE, type(exc).__name__)) from None
    return data, CN.sha256_bytes(data)


def _rebuild(data: bytes, sha: str, side: dict):
    """Step 4: the snapshot from the bytes and the side channel, with the runtime round trip and the removed-row cross-check."""
    try:
        snap = SN.load_snapshot(data, side)
        problems = SN.roundtrip_problems(snap, data, sha, side)
    except Exception as exc:
        raise SN.AdmissionRefused(SN.SNAPSHOT_ROUNDTRIP_FAILED, (SN.SNAPSHOT_ROUNDTRIP_FAILED, type(exc).__name__)) from None
    if problems:
        _refuse(SN.SNAPSHOT_ROUNDTRIP_FAILED, (SN.SNAPSHOT_ROUNDTRIP_FAILED, *problems))
    if not _mda_contract_ok(_TableProvider.manifest(snap)):
        _refuse(SN.CONTRACT_MISMATCH)
    return snap


def _admit_mda(p, kind: str, root):
    """Steps 2-9 on an MD-A table provider (every read a class-level call, never an instance attribute)."""
    m = _TableProvider.manifest(p)
    if not _mda_contract_ok(m):
        _refuse(SN.CONTRACT_MISMATCH)
    data, sha = _canonical(p)
    if sha != m.content_sha256:
        _refuse(SN.CONTENT_HASH_STALE)
    side = SN.side_channel_of(p)
    snap = _rebuild(data, sha, side)
    sm = _TableProvider.manifest(snap)
    if not (isinstance(sm.start_session, str) and isinstance(sm.end_session, str)):
        _refuse(SN.BRIDGE_UNMAPPABLE, (SN.BRIDGE_UNMAPPABLE, "DATASET_SPAN_UNKNOWN"))
    record, decision = _record_and_decision(snap, sha)
    side_sha = SN.side_channel_sha256(side)
    entry = None
    if sm.kind == HD.SYNTHETIC:
        entry = _lookup(kind, sha)
        if entry is None:
            raise SN.AdmissionRefused(SN.SYNTHETIC_NOT_PINNED, [SN.SYNTHETIC_NOT_PINNED, SN.LICENCE_EVIDENCE_UNDEFINED, *decision["reasons"]],
                                      decision=decision, fingerprint=sha)
        problem = _guard_problem(entry, kind, _mda_counts(snap), _mda_ids(snap), side_sha)
        if problem:
            _refuse(SN.ALLOWLIST_GUARD_FAILED, (problem,), decision)
    else:
        LE.check(sm, decision)
    if decision["usable_for"] != AC.EVALUATION:
        _refuse(SN.NOT_EVALUATION, (SN.NOT_EVALUATION, *decision["reasons"]), decision)
    return _issue(_mda_fields(sm, kind, sha, record, decision, side_sha, entry, root), snap, root, "MDA")


def admit(provider) -> AdmittedDataset:
    """An MD-A SyntheticProvider (exact type, no instance attribute beyond the table provider's own) -> an AdmittedDataset, or a refusal."""
    if type(provider) is not SY.SyntheticProvider or set(vars(provider)) != SN.SNAPSHOT_ATTRS:
        raise SN.AdmissionRefused(SN.NOT_A_REGISTERED_PROVIDER, (SN.NOT_A_REGISTERED_PROVIDER,))
    return _guarded(None, lambda: _admit_mda(provider, KIND_OF_OBJECT["SYNTHETIC"], None))


def admit_from_source(adapter_id, root) -> AdmittedDataset:
    """The gate builds the MD-A adapter itself from `root` (MS-1: the parent of raw/; sip_daily_v1: the bundle folder) inside the guard, with
    read-only access under that root. Refused inside an active guard (a nested guard with a root)."""
    if adapter_id not in SOURCE_ADAPTERS:
        raise SN.AdmissionRefused(SN.NOT_A_REGISTERED_PROVIDER, (SN.NOT_A_REGISTERED_PROVIDER,))
    try:
        r = Path(root).resolve(strict=True)
        ok = r.is_dir()
    except (OSError, RuntimeError, TypeError, ValueError):
        ok = False
    if not ok:
        raise SN.AdmissionRefused(SN.SOURCE_UNREADABLE, (SN.SOURCE_UNREADABLE, "ROOT"))

    def body():
        try:
            if adapter_id == MS1.ADAPTER_VERSION:
                p = MS1.Ms1EvidenceProvider(r / "raw")
            else:
                p = SIP.SipDailyV1Provider(r)
        except (MS1.SourceFormatError, MS1.SourceIntegrityError, OSError, ValueError, KeyError, TypeError, EOFError, zlib.error) as exc:
            raise SN.AdmissionRefused(SN.SOURCE_UNREADABLE, (SN.SOURCE_UNREADABLE, type(exc).__name__)) from None
        if set(vars(p)) != SN.SNAPSHOT_ATTRS:
            _refuse(SN.NOT_A_REGISTERED_PROVIDER)
        return _admit_mda(p, KIND_OF_OBJECT[adapter_id], r)
    return _guarded(r, body)


def _double_ids(d) -> list:
    return sorted(set(d._instruments) | set(d._bars))


def _double_counts(d):
    return len(d._instruments), sum(len(v) for v in d._bars.values()), ((d._sessions[0], d._sessions[-1]) if d._sessions else (None, None))


def _double_fields(m, fp, entry) -> dict:
    base = md_eval.CONTRACT_BASE_OF.get(m.contract_version) or (None, None)
    return dict(admission_version=ADMISSION_VERSION, dataset_id=m.dataset_id, admitted_as="SYNTHETIC_DOUBLE", kind_of_object=KIND_OF_OBJECT["DOUBLE"],
                content_sha256=m.content_sha256, fingerprint=fp, contract_version=m.contract_version, contract_sha256=m.contract_sha256,
                contract_base_sha256=base[1], criteria_version=None, record_sha256=None, decision_json=None, decision_sha256=None,
                side_channel_sha256=None, allowlist_entry_id=entry["allowlist_entry_id"] if entry is not None else None, licence_evidence_sha256=None,
                source_root_id=None, source_files=())


def _admit_double(d):
    m = d._manifest
    if not _double_contract_ok(m):
        _refuse(SN.CONTRACT_MISMATCH)
    if getattr(m, "kind", None) != "SYNTHETIC":
        _refuse(SN.DOUBLE_HISTORICAL_NOT_ADMITTED)
    if not SN.double_types_ok(d):
        _refuse(SN.SNAPSHOT_ROUNDTRIP_FAILED, (SN.SNAPSHOT_ROUNDTRIP_FAILED, "RECORD_TYPES"))
    try:
        fp0 = SN.double_fingerprint(d)
        snap = SN.snapshot_double(d)
        same = SN.double_fingerprint(snap) == fp0
    except Exception as exc:
        raise SN.AdmissionRefused(SN.SNAPSHOT_ROUNDTRIP_FAILED, (SN.SNAPSHOT_ROUNDTRIP_FAILED, type(exc).__name__)) from None
    if not same:
        _refuse(SN.SNAPSHOT_ROUNDTRIP_FAILED, (SN.SNAPSHOT_ROUNDTRIP_FAILED, "FINGERPRINT"))
    kind = KIND_OF_OBJECT["DOUBLE"]
    entry = _lookup(kind, fp0)
    if entry is None:
        raise SN.AdmissionRefused(SN.SYNTHETIC_NOT_PINNED, [SN.SYNTHETIC_NOT_PINNED, SN.LICENCE_EVIDENCE_UNDEFINED], fingerprint=fp0)
    problem = _guard_problem(entry, kind, _double_counts(snap), _double_ids(snap), None)
    if problem:
        _refuse(SN.ALLOWLIST_GUARD_FAILED, (problem,))
    return _issue(_double_fields(snap._manifest, fp0, entry), snap, None, "MDB")


def admit_double(double) -> AdmittedDataset:
    """An MD-B ContractDouble (exact type), SYNTHETIC and pinned, -> an AdmittedDataset over a deep copy; a HISTORICAL double always refuses."""
    if type(double) is not ContractDouble:
        raise SN.AdmissionRefused(SN.NOT_A_REGISTERED_PROVIDER, (SN.NOT_A_REGISTERED_PROVIDER,))
    return _guarded(None, lambda: _admit_double(double))


# ---- the re-verify path and verify (Part A 2.7, 3.3) --------------------------------------------------------------------------------------
COMPARED_FIELDS = ("content_sha256", "fingerprint", "kind_of_object", "contract_version", "contract_sha256", "contract_base_sha256", "criteria_version",
                   "record_sha256", "decision_sha256", "side_channel_sha256", "allowlist_entry_id", "admitted_as", "source_root_id", "source_files")


def _recompute(ds):
    """Every Admission field recomputed from the gate's snapshot (no refusal for a lost lookup: the field comparison reports it)."""
    snap, root = ds._snapshot, ds._root
    if ds._flavour == "MDA":
        m = _TableProvider.manifest(snap)
        data, sha = _canonical(snap)
        side = SN.side_channel_of(snap)
        record, decision = _record_and_decision(snap, sha)
        side_sha = SN.side_channel_sha256(side)
        kind = _kind_for(m, root)
        entry = _lookup(kind, sha) if m.kind == HD.SYNTHETIC else None
        if entry is not None and _guard_problem(entry, kind, _mda_counts(snap), _mda_ids(snap), side_sha):
            entry = None
        return _mda_fields(m, kind, sha, record, decision, side_sha, entry, root), decision
    fp = SN.double_fingerprint(snap)
    kind = KIND_OF_OBJECT["DOUBLE"]
    entry = _lookup(kind, fp)
    if entry is not None and _guard_problem(entry, kind, _double_counts(snap), _double_ids(snap), None):
        entry = None
    return _double_fields(snap._manifest, fp, entry), None


def _compare_admission_fields(adm, fields: dict) -> None:
    """Re-verify step 3: each stored field against its recomputed value, the stored decision bytes against their stored sha and against the
    recomputed bytes, then the admission sha recomputed from the recomputed fields."""
    bad = [f for f in COMPARED_FIELDS if getattr(adm, f) != fields[f]]
    try:
        if adm.decision_json is not None and CN.sha256_bytes(adm.decision_json) != adm.decision_sha256:
            bad.append("decision_json_sha256")
    except TypeError:
        bad.append("decision_json_sha256")
    if adm.decision_json != fields["decision_json"]:
        bad.append("decision_json")
    if _admission_sha(fields) != adm.admission_sha256:
        bad.append("admission_sha256")
    if bad:
        raise SN.NotAdmitted(SN.ADMISSION_MISMATCH, (SN.ADMISSION_MISMATCH, *bad))


def _query_state_unchanged(ds) -> bool:
    try:
        return SN.query_state_sha256(ds._snapshot) == ds._qsha
    except Exception:
        return False


def _require_issued(ds) -> None:
    if not (_issued_dataset(ds) and _issued_admission(ds._admission)):
        raise SN.NotAdmitted(SN.NOT_ADMITTED)


def _reverify(ds) -> None:
    _require_issued(ds)
    if not _query_state_unchanged(ds):
        raise SN.AdmissionRefused(SN.SNAPSHOT_CHANGED, (SN.SNAPSHOT_CHANGED,))
    fields, _ = _guarded(None, lambda: _recompute(ds))
    _compare_admission_fields(ds._admission, fields)
    _READ_LOG.append(("REVERIFY_READ", ds._admission.kind_of_object, ds._admission.dataset_id))


def admit_for_engine(obj, *, admission=None) -> AdmittedDataset:
    """md_eval.engine.run's dispatcher: an issued AdmittedDataset is re-verified; a ContractDouble or a SyntheticProvider is admitted on the
    spot; anything else refuses. A presented `admission` must be an issued Admission with the same admission sha256."""
    if type(obj) is AdmittedDataset:
        _reverify(obj)
        ds = obj
    elif type(obj) is ContractDouble:
        ds = admit_double(obj)
    elif type(obj) is SY.SyntheticProvider:
        ds = admit(obj)
    else:
        raise SN.AdmissionRefused(SN.NOT_A_REGISTERED_PROVIDER, (SN.NOT_A_REGISTERED_PROVIDER,))
    if admission is not None:
        if not _issued_admission(admission):
            raise SN.NotAdmitted(SN.NOT_ADMITTED)
        if admission.admission_sha256 != ds._admission.admission_sha256:
            raise SN.NotAdmitted(SN.ADMISSION_MISMATCH)
    return ds


def verify(ds) -> None:
    """The snapshot is what was admitted (exact type, attribute set, frozen indexes, query-state hash) and, for a source-built dataset, every
    source file still has its recorded bytes. Called by engine.run before and after the run and by require_admitted, never inside a guard."""
    _require_issued(ds)
    root = ds._root

    def body():
        if not SN.structure_ok(ds._snapshot) or not _query_state_unchanged(ds):
            _refuse(SN.SNAPSHOT_CHANGED)
        if root is not None and SN.source_changes(root, ds._admission.source_files):
            _refuse(SN.SOURCE_CHANGED_DURING_RUN)
    _guarded(root, body)
    _READ_LOG.append(("VERIFY_READ", ds._admission.kind_of_object, ds._admission.dataset_id))


def require_admitted(provider) -> None:
    """E7 (benchmarks): an issued AdmittedDataset whose snapshot is unchanged (in memory), then verify(); anything else NOT_ADMITTED."""
    _require_issued(provider)
    if not _query_state_unchanged(provider):
        raise SN.AdmissionRefused(SN.SNAPSHOT_CHANGED, (SN.SNAPSHOT_CHANGED,))
    verify(provider)


def admitted_manifest(ds):
    """The admitted dataset's md_eval-shaped manifest, read without a recorder call."""
    _require_issued(ds)
    if ds._flavour == "MDB":
        return ds._snapshot._manifest
    return BR.manifest(_TableProvider.manifest(ds._snapshot))


def manifest_view(x):
    """Manifest only, no data read: an AdmittedDataset's admitted manifest, else the bridge's manifest-only view (Part A 4.2)."""
    if type(x) is AdmittedDataset:
        return admitted_manifest(x)
    return BR.manifest_view_raw(x)


def identity_fields(ds) -> dict:
    """What a run identity records of its admission; the decision flags and caps come from the decision RECOMPUTED from the snapshot."""
    _require_issued(ds)
    adm = ds._admission
    decision = _guarded(None, lambda: _recompute(ds))[1] if ds._flavour == "MDA" else None
    return {"admission_sha256": adm.admission_sha256, "admitted_as": adm.admitted_as, "decision_sha256": adm.decision_sha256,
            "allowlist_entry_id": adm.allowlist_entry_id, "decision_flags": sorted(decision["flags"]) if decision else [],
            "verdict_caps": sorted(c["flag"] for c in decision["verdict_caps"]) if decision else []}


def allowlist_problems(entries=None) -> list:
    """The table's integrity problems (I1-S12): duplicate fingerprints per kind, reused entry ids, counts over the caps."""
    entries = SAL.ENTRIES if entries is None else entries
    out, ids, fps = [], set(), set()
    for e in entries:
        eid, key = e.get("allowlist_entry_id"), (e.get("kind_of_object"), e.get("fingerprint"))
        if eid in ids:
            out.append(("REUSED_ENTRY_ID", eid))
        if key in fps:
            out.append(("DUPLICATE_FINGERPRINT", eid))
        ids.add(eid)
        fps.add(key)
        caps = SAL.CAPS.get(e.get("kind_of_object"))
        if caps is None or e.get("instrument_count", 0) > caps["max_instruments"] or e.get("bar_count", 0) > caps["max_bars"]:
            out.append(("COUNT_OVER_CAP", eid))
    return out
