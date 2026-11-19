"""
I-1B-1 (S4): the research admission gate - admission, the gate order and isolation of md_eval's entry points (Part C 4.1).
  P01-P03  admitted runs: the MD-A twin through the bridge, the MD-B double, a registered VALIDATION run
  A01-A09  forged / duck-typed admissions, mismatched pairings, edited stored fields, the contract, exact types, the record check, an
           instance-level manifest override, an empty dataset
  L01-L06  licence: every HISTORICAL admission refuses (ID-8); a BLOCK record; NOT_PERMITTED is not evaluation; HISTORICAL_EVIDENCED reserved
  G01-G02  the gate order (a segment refusal reads nothing) and the call-recorder forwarding
  O01-O06  offline() encloses the run; the import-time side effects (a fresh interpreter); nested offline attempts; the read guard; .pyc
           writes under the guard (a fresh interpreter without -B); the calendar warm-up
  V01-V05  md_eval's signal sources and benchmarks refuse what the gate did not issue; the verdict's consistency check; the caps union
  D01      V1 through the bridge keeps its flags and its frozen pre-registration hash
Every refusal asserts its exact code. Synthetic data only; no network, no database, no real market data.
"""
import ast
import dataclasses
import json
import os
import socket
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

import i1_builders as I1
import md_eval_fixtures as FX
import research_gate as RG
import swing_pullback as SP
from historical_data import acceptance as AC
from historical_data import calendar as CAL
from historical_data import canonical as CN
from historical_data import schema as HSC
from historical_data import validate as VA
import historical_data_fixtures as HF
from md_eval import benchmarks as BM
from md_eval import engine as EN
from md_eval import prereg as PR
from md_eval import registry as RGY
from md_eval import verdict as VD
from md_eval.contract import LicenceRefused, UnsupportedContract, licence_gate
from md_eval.double import ContractDouble
from md_eval.isolation import IsolationViolation
from md_eval.signals import BuyAndHold, EqualWeight, RandomEntry, Signal, SwingBridge
from md_eval.view import BookSnapshot, DecisionContext
RGA = sys.modules["research_gate.admit"]                                        # the module: the package attribute `admit` is the admit() function
from research_gate import synthetic_allowlist as SAL
import md_eval

ROOT = Path(__file__).resolve().parents[1]
HEX = set("0123456789abcdef")
CONFIRMED = HSC.Licence(licence_ref="owner-file", personal_research_use="CONFIRMED", licence_file_sha256="f" * 64, confirmed_by="owner",
                        confirmed_on="2026-11-16")


def _code(exc_info):
    return exc_info.value.code


def _src(sessions):
    return FX.ScriptedSource(sessions)


def _cfg(ds, **kw):
    s = ds.sessions("0000-00-00", "9999-99-99")
    return EN.RunConfig(start_session=s[0], end_session=s[-1], **kw)


def _reseal(p, **manifest_over):
    """Test-only reach-in: the provider's manifest edited and its content sha recomputed (otherwise the code would be CONTENT_HASH_STALE)."""
    p._manifest = dataclasses.replace(p._manifest, **manifest_over)
    p._manifest = dataclasses.replace(p._manifest, content_sha256=CN.content_sha256(p.canonical_document()))
    return p


def _small(**kw):
    inst, bars = HF.series("AAA", HF.S[:10], [100.0 + k for k in range(10)], **kw)
    return HF.provider((inst, bars))


@pytest.fixture(scope="module")
def twin_run():
    adm = RG.admit(I1.pb_mda_v1_twin())
    return adm, EN.run(adm, SwingBridge(True, "SYNTHETIC_UNIVERSE"), _cfg(adm))


# ============================================================================================================ admitted runs (P)
def test_P01_the_md_a_twin_runs_through_the_bridge_with_its_decision_caps_and_flags(twin_run):
    adm, r = twin_run
    a = r["identity"]["admission"]
    assert a["admitted_as"] == "SYNTHETIC_PINNED" and len(a["admission_sha256"]) == 64 and set(a["admission_sha256"]) <= HEX
    caps = {c["flag"] for c in adm.admission.decision["verdict_caps"]}
    assert caps and set(a["verdict_caps"]) == caps and caps <= set(r["flags"])
    assert "EARNINGS_DATA_UNAVAILABLE" in r["flags"] and any(x["action"] == "ENTRY" for x in r["decisions"])


def test_P02_the_md_b_double_runs_admitted():
    d = FX.v1_double()
    r = EN.run(d, SwingBridge(True, FX.UNIVERSE), FX.v1_config(d))
    assert r["identity"]["admission"]["admitted_as"] == "SYNTHETIC_DOUBLE" and r["identity"]["admission"]["verdict_caps"] == []


def test_P03_a_synthetic_double_naming_a_registered_preregistration_runs_and_is_recorded(tmp_path):
    import test_md_eval_registry_verdict as TRV
    w = TRV._world(tmp_path)
    r = EN.run(w["d"], FX.ScriptedSource(w["s"], role="STRATEGY", name="S1"), TRV._cfg(w, "VALIDATION", 10, 29), registry=w["reg"], holdout=w["led"])
    assert w["led"].run_entry("VALIDATION_RUN", w["sha"], w["d"].manifest().content_sha256, r["identity_sha256"]) is not None


# ============================================================================================================ forged and mismatched admissions (A)
def _field_copy(cls, issued):
    f = object.__new__(cls)
    for s in RGA.ADMISSION_FIELDS:                                              # every slot except __weakref__ (declared, not writable)
        object.__setattr__(f, s, object.__getattribute__(issued, s))
    return f


def test_A01_forged_admissions_are_refused_on_both_engine_paths():
    adm_v1 = RG.admit_double(FX.v1_double())
    issued = adm_v1.admission
    with pytest.raises(RG.NotAdmitted) as e:
        RG.Admission(object())
    assert _code(e) == "NOT_ADMITTED"
    s = adm_v1.sessions("0", "9")
    for forged in (object.__new__(RG.Admission), _field_copy(RG.Admission, issued)):
        for provider in (adm_v1, FX.v1_double()):
            with pytest.raises(RG.NotAdmitted) as e:
                EN.run(provider, _src(s), _cfg(adm_v1), admission=forged)
            assert _code(e) == "NOT_ADMITTED"


def test_A01d_a_subclass_forgery_with_a_true_eq_is_refused_by_the_exact_type_check():
    class Fake(RG.Admission):
        __slots__ = ()
        __eq__ = lambda s, o: True  # noqa: E731
        __hash__ = object.__hash__
    adm_v1 = RG.admit_double(FX.v1_double())
    fake = _field_copy(Fake, adm_v1.admission)
    assert fake == adm_v1.admission                                             # equal by its own __eq__, yet refused
    s = adm_v1.sessions("0", "9")
    for provider in (adm_v1, FX.v1_double()):
        with pytest.raises(RG.NotAdmitted) as e:
            EN.run(provider, _src(s), _cfg(adm_v1), admission=fake)
        assert _code(e) == "NOT_ADMITTED"


def test_A02_a_genuine_admission_of_another_dataset_is_a_mismatch():
    a, b = RG.admit_double(FX.v1_double()), RG.admit_double(I1.pb_v1_truncate_240())
    with pytest.raises(RG.NotAdmitted) as e:
        EN.run(b, _src(b.sessions("0", "9")), _cfg(b), admission=a.admission)
    assert _code(e) == "ADMISSION_MISMATCH"


class _SpyLedger:
    def __init__(self):
        self.recorded = []

    def head(self):
        return None

    def record_validation_run(self, *a, **k):
        self.recorded.append(a)

    record_test_run = record_validation_run


def _edited_decision(adm):
    d = adm.admission.decision
    d["verdict_caps"] = []
    return CN.dumps(d)


def test_A03_an_edited_stored_field_is_an_admission_mismatch():
    adm = RG.admit(I1.pb_mda_v1_twin())
    other = next(e["allowlist_entry_id"] for e in SAL.ENTRIES if e["allowlist_entry_id"] != adm.admission.allowlist_entry_id)
    object.__setattr__(adm.admission, "allowlist_entry_id", other)
    with pytest.raises(RG.NotAdmitted) as e:
        EN.run(adm, _src(adm.sessions("0", "9")), _cfg(adm))
    assert _code(e) == "ADMISSION_MISMATCH"


def test_A03b_an_edited_decision_json_is_refused_and_nothing_is_recorded():
    adm = RG.admit(I1.pb_mda_v1_twin())
    object.__setattr__(adm.admission, "decision_json", _edited_decision(adm))
    spy = _SpyLedger()
    with pytest.raises(RG.NotAdmitted) as e:
        EN.run(adm, _src(adm.sessions("0", "9")), _cfg(adm), holdout=spy)
    assert _code(e) == "ADMISSION_MISMATCH" and spy.recorded == []


def test_A03c_an_edited_decision_sha256_is_refused():
    adm = RG.admit(I1.pb_mda_v1_twin())
    object.__setattr__(adm.admission, "decision_sha256", "0" * 64)
    with pytest.raises(RG.NotAdmitted) as e:
        EN.run(adm, _src(adm.sessions("0", "9")), _cfg(adm))
    assert _code(e) == "ADMISSION_MISMATCH"


def test_A03d_the_run_identity_reads_the_recomputed_decision_not_the_stored_one(monkeypatch):
    adm = RG.admit(I1.pb_mda_v1_twin())
    want = sorted(c["flag"] for c in adm.admission.decision["verdict_caps"])
    object.__setattr__(adm.admission, "decision_json", _edited_decision(adm))
    monkeypatch.setattr(RGA, "_compare_admission_fields", lambda a, f: None)  # a test-only mutant of re-verify step 3
    r = EN.run(adm, _src(adm.sessions("0", "9")), _cfg(adm))
    assert want and r["identity"]["admission"]["verdict_caps"] == want


def test_A04_a_changed_criteria_version_after_admission_is_a_mismatch(monkeypatch):
    adm = RG.admit(I1.pb_mda_v1_twin())
    monkeypatch.setattr(AC, "CRITERIA_VERSION", "historical_data_acceptance_criteria_v999")
    with pytest.raises(RG.NotAdmitted) as e:
        EN.run(adm, _src(adm.sessions("0", "9")), _cfg(adm))
    assert _code(e) == "ADMISSION_MISMATCH"


def test_A05_a_wrong_contract_sha_is_a_contract_mismatch():
    d = FX.v1_double(sha="0" * 64)
    with pytest.raises(RG.AdmissionRefused) as e:
        RG.admit_double(d)
    assert _code(e) == "CONTRACT_MISMATCH"
    with pytest.raises(UnsupportedContract):
        EN.run(d, _src(d.sessions("0", "9")), FX.v1_config(d))                   # the engine refuses earlier still (check_version)
    p = I1.pb_mda_lookahead_world()
    p._manifest = dataclasses.replace(p._manifest, contract_sha256="0" * 64)    # test-only reach-in on the contract fields
    with pytest.raises(RG.AdmissionRefused) as e:
        RG.admit(p)
    assert _code(e) == "CONTRACT_MISMATCH"


def test_A06_duck_types_subclasses_and_extra_instance_attributes_are_not_registered_providers():
    d = FX.v1_double()

    class Duck:
        def __getattr__(self, name):
            return getattr(d, name)

        def manifest(self):
            return d._manifest

    class SubDouble(ContractDouble):
        pass

    class SubProvider(I1.SY.SyntheticProvider):
        pass
    with pytest.raises(RG.AdmissionRefused) as e:
        EN.run(Duck(), _src(d.sessions("0", "9")), FX.v1_config(d))
    assert _code(e) == "NOT_A_REGISTERED_PROVIDER"
    sub = SubDouble(sessions=d._sessions, instruments=list(d._instruments.values()), bars=[b for v in d._bars.values() for b in v], manifest=d._manifest)
    with pytest.raises(RG.AdmissionRefused) as e:
        RG.admit_double(sub)
    assert _code(e) == "NOT_A_REGISTERED_PROVIDER"
    inst, bars = HF.series("AAA", HF.S[:10], [100.0 + k for k in range(10)])
    with pytest.raises(RG.AdmissionRefused) as e:
        RG.admit(SubProvider(instruments=[inst], bars=bars))
    assert _code(e) == "NOT_A_REGISTERED_PROVIDER"
    p = I1.pb_mda_lookahead_world()
    p.extra = 1
    with pytest.raises(RG.AdmissionRefused) as e:
        RG.admit(p)
    assert _code(e) == "NOT_A_REGISTERED_PROVIDER"


def test_A07_the_record_check_recomputes_the_verdict_the_record_sha_and_the_content_shas():
    p = I1.pb_mda_lookahead_world()
    rec = VA.validate(p)
    sha = p.manifest().content_sha256
    RGA._check_record(rec, sha)
    for edit, code in ((lambda r: r.pop("verdict"), "RECORD_INVALID"), (lambda r: r.__setitem__("verdict", "MAYBE"), "RECORD_INVALID"),
                       (lambda r: r.__setitem__("record_sha256", "0" * 64), "RECORD_INVALID"), (lambda r: r.__setitem__("verdict", "BLOCK"), "DATASET_BLOCKED")):
        bad = json.loads(json.dumps(rec))
        edit(bad)
        with pytest.raises(RG.AdmissionRefused) as e:
            RGA._check_record(bad, sha)
        assert _code(e) == code
    with pytest.raises(RG.AdmissionRefused) as e:
        RGA._check_record(rec, "0" * 64)
    assert _code(e) == "RECORD_INVALID"


def test_A08_an_instance_level_manifest_override_is_an_admission_mismatch():
    d = I1.pb_flat5()
    other = dataclasses.replace(d._manifest, dataset_id="another-dataset")
    d.manifest = lambda: other
    with pytest.raises(RG.NotAdmitted) as e:
        EN.run(d, _src(d.sessions("0", "9")), FX.v1_config(d))
    assert _code(e) == "ADMISSION_MISMATCH"


def test_A09_an_empty_synthetic_dataset_is_bridge_unmappable():
    with pytest.raises(RG.AdmissionRefused) as e:
        RG.admit(I1.SY.SyntheticProvider(instruments=[], bars=[]))
    assert _code(e) == "BRIDGE_UNMAPPABLE"


# ============================================================================================================ licence (L)
def test_L01_a_resealed_historical_confirmed_dataset_passes_the_licence_gate_and_is_refused_admission():
    p = _reseal(_small(), kind="HISTORICAL", licence=CONFIRMED)
    assert licence_gate(RG.manifest_view(p)) == "CONFIRMED"
    with pytest.raises(RG.AdmissionRefused) as e:
        RG.admit(p)
    assert _code(e) == "LICENCE_EVIDENCE_UNDEFINED"


def test_L02_an_unknown_licence_is_refused_by_the_gate_and_by_admission():
    inst, bars = HF.series("AAA", HF.S[:10], [100.0 + k for k in range(10)])
    p = HF.provider((inst, bars), kind="HISTORICAL")
    with pytest.raises(LicenceRefused):
        licence_gate(RG.manifest_view(p))
    with pytest.raises(RG.AdmissionRefused) as e:
        RG.admit(p)
    assert _code(e) == "LICENCE_EVIDENCE_UNDEFINED" and {"LICENCE_EVIDENCE_UNDEFINED", "LICENCE_NOT_CONFIRMED"} <= set(e.value.reasons)


def test_L03_a_blocked_record_refuses_before_the_licence_step():
    p = _reseal(_small(known_at=CAL.close_utc(HF.S[0])), kind="HISTORICAL", licence=CONFIRMED)   # every bar KNOWN_AT_MISMATCH but the first
    assert licence_gate(RG.manifest_view(p)) == "CONFIRMED"
    with pytest.raises(RG.AdmissionRefused) as e:
        RG.admit(p)
    assert _code(e) == "DATASET_BLOCKED"


def test_L04_a_not_permitted_synthetic_dataset_is_not_evaluation():
    with pytest.raises(RG.AdmissionRefused) as e:
        RG.admit(I1.pb_mda_not_permitted())
    assert _code(e) == "NOT_EVALUATION"


def test_L06_historical_evidenced_is_reserved_once_and_never_constructed():
    consts, loads, calls = [], [], []
    for f in sorted((ROOT / "research_gate").glob("*.py")):
        tree = ast.parse(f.read_text(encoding="utf-8"))
        consts += [f.name for n in ast.walk(tree) if isinstance(n, ast.Constant) and n.value == "HISTORICAL_EVIDENCED"]
        loads += [f.name for n in ast.walk(tree) if isinstance(n, ast.Name) and n.id == "HISTORICAL_EVIDENCED" and isinstance(n.ctx, ast.Load)]
        for c in ast.walk(tree):
            if isinstance(c, ast.Call) and isinstance(c.func, ast.Name) and c.func.id == "Admission":
                calls += [n for n in ast.walk(c) if isinstance(n, (ast.Name, ast.Constant)) and "HISTORICAL_EVIDENCED" in (getattr(n, "id", "") or str(getattr(n, "value", "")))]
    assert consts == ["admit.py"] and loads == [] and calls == []
    assert RG.ADMITTED_AS_REACHABLE == ("SYNTHETIC_PINNED", "SYNTHETIC_DOUBLE")


# ============================================================================================================ the gate order (G)
def test_G01_a_segment_refused_run_reads_nothing():
    d = I1.pb_flat5()
    before = RG.read_log()
    with pytest.raises(EN.RunRefused):
        EN.run(d, _src(d.sessions("0", "9")), FX.v1_config(d, segment_label="VALIDATION"))
    assert RG.read_log() == before and d.calls == ["manifest"]


def test_G02_the_callers_recorders_are_populated_by_an_admitted_run():
    a, b = I1.pb_flat5(), I1.pb_flat5()
    s = a.sessions("0", "9")
    src = lambda: FX.ScriptedSource(s, entries={1: [Signal("SIM:X", notional=1000.0)]}, exits={3: ["SIM:X"]})  # noqa: E731
    EN.run(a, src(), FX.v1_config(a))
    EN.run(b, src(), FX.v1_config(b))
    assert a.calls[0] == "manifest" and a.calls == b.calls and a.exec_calls and a.exec_calls == b.exec_calls


# ============================================================================================================ isolation (O)
class _SocketSpy:
    def manifest(self):
        socket.socket()


def test_O01_offline_encloses_the_whole_run_including_the_manifest():
    with pytest.raises(IsolationViolation):
        EN.run(_SocketSpy(), _src(["2024-01-01"]), EN.RunConfig(start_session="2024-01-01", end_session="2024-01-01"))


O02_CHILD = r'''
import ast, json, logging, os, sys, threading
from pathlib import Path
ROOT = sys.argv[1]
sys.path[:] = [ROOT if p == "" else p for p in sys.path]
import swing_pullback, swing_pullback.core, strategy_validation.dataset, strategy_validation.models, rotation_backtest.metrics
import md_eval, md_eval.contract, md_eval.double, md_eval.isolation, md_eval.costs, md_eval.ledger, md_eval.view, md_eval.prereg, md_eval.registry
import md_eval.metrics, md_eval.inference, md_eval.book, md_eval.cash_rate, md_eval.prior_trials
from md_eval.isolation import offline
problems = []
state = lambda: (threading.active_count(), list(sys.path), list(sys.meta_path), list(logging.root.handlers), dict(os.environ))
before, snap = set(sys.modules), state()
events, phase = [], ["P1"]
FORBIDDEN = ("socket.", "sqlite3.connect", "subprocess.Popen", "os.system", "_thread.start_new_thread")
def rec(ev, args):
    if phase[0] is None:
        return
    if ev in ("open", "os.listdir", "os.scandir", "sys.addaudithook", "research_gate.selftest") or ev.startswith(FORBIDDEN):
        a0 = args[0] if args else None
        events.append((phase[0], ev, os.fsdecode(a0) if isinstance(a0, (str, bytes)) else (str(a0) if a0 is not None else None),
                       args[1] if ev == "open" else None, args[2] if ev == "open" else None))
sys.addaudithook(rec)
WRITE = os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_APPEND | os.O_TRUNC
def roots():
    return [Path(p).resolve() for p in sys.path if isinstance(p, str)]
def under(p, bases):
    return any(p == b or p.is_relative_to(b) for b in bases)
def tz_ok(p):
    import importlib.util
    spec = importlib.util.find_spec("tzdata")
    for d in ([Path(x).resolve() for x in spec.submodule_search_locations] if spec else []):
        if under(p, [d]):
            return True
        try:
            if p.relative_to(d.parent).parts[0].lower().startswith("tzdata-") and p.relative_to(d.parent).parts[0].lower().endswith(".dist-info"):
                return True
        except (ValueError, IndexError):
            pass
    return False
def check(ph, allow_tz):
    bases = roots()
    for p_, ev, path, mode, flags in [e for e in events if e[0] == ph]:
        if ev.startswith(FORBIDDEN):
            problems.append([ph, "forbidden", ev])
        elif ev == "open":
            p = Path(path).resolve() if path else None
            write = (isinstance(mode, str) and any(c in mode for c in "wax+")) or (isinstance(flags, int) and flags & WRITE)
            ok = p is not None and not write and ((p.suffix.lower() in (".py", ".pyc") and under(p, bases)) or (allow_tz and (tz_ok(p) or p in bases)))
            if not ok:
                problems.append([ph, "open", path, mode])
        elif ev in ("os.listdir", "os.scandir"):
            p = Path(path if path not in (None, "None") else ".").resolve()
            if not (under(p, bases) or (allow_tz and tz_ok(p))):
                problems.append([ph, ev, path])
def refuse_bound(mods):
    out = []
    def bad(x):
        return callable(x) and getattr(x, "__qualname__", "") == "offline.<locals>.refuse"
    for name in sorted(mods):
        m = sys.modules.get(name)
        if m is None:
            continue
        for k, v in list(vars(m).items()):
            vals = [v]
            if isinstance(v, type):
                vals += list(vars(v).values())
            for f in list(vals):
                f = getattr(f, "__func__", f)
                for x in [f] + list(getattr(f, "__defaults__", None) or ()) + list((getattr(f, "__kwdefaults__", None) or {}).values()) + \
                        [c.cell_contents for c in (getattr(f, "__closure__", None) or ()) if hasattr(c, "cell_contents")]:
                    if bad(x):
                        out.append([name, k])
    return out
def env_reads(mods):
    out = []
    for name in sorted(mods):
        f = getattr(sys.modules.get(name), "__file__", None)
        if not f or not f.endswith(".py") or name.split(".")[0] in sys.stdlib_module_names:
            continue
        tree = ast.parse(Path(f).read_text(encoding="utf-8"))
        for n in ast.walk(tree):
            if isinstance(n, ast.Attribute) and isinstance(n.value, ast.Name) and n.value.id == "os" and n.attr in ("environ", "getenv", "environb"):
                out.append([name, n.attr])
            if isinstance(n, ast.ImportFrom) and n.module == "os" and any(a.name in ("environ", "getenv", "environb") for a in n.names):
                out.append([name, "from os import"])
    return out
with offline() as att1:
    import research_gate
    import md_eval.engine, md_eval.signals, md_eval.benchmarks, md_eval.verdict
phase[0] = None
delta1 = set(sys.modules) - before
if att1:
    problems.append(["P1", "offline attempts", att1])
check("P1", False)
if state() != snap:
    problems.append(["P1", "process state changed"])
new1 = {m.split(".")[0] for m in delta1 if m.split(".")[0] not in before} - set(sys.stdlib_module_names)
if not new1 <= {"research_gate", "historical_data", "research_data", "value_quality"}:
    problems.append(["P1", "delta", sorted(new1)])
problems += [["P1", "refuse bound"] + x for x in refuse_bound(delta1)] + [["P1", "env read"] + x for x in env_reads(delta1)]
hooks = [e for e in events if e[0] == "P1" and e[1] == "sys.addaudithook"]
selftests = [e for e in events if e[0] == "P1" and e[1] == "research_gate.selftest"]
s = getattr(sys, "_research_gate_audit_hook", None)
RGA = sys.modules["research_gate.admit"]
if len(hooks) != 1 or len(selftests) != 1 or not (type(s) is tuple and len(s) == 2 and callable(s[0]) and type(s[1]).__name__ == "ContextVar" and RGA._GUARD is s[1]):
    problems.append(["P1", "audit hook install", len(hooks), len(selftests)])
for tree_mod in [m for m in delta1 if m.startswith("research_gate")]:
    f = sys.modules[tree_mod].__file__
    for n in ast.walk(ast.parse(Path(f).read_text(encoding="utf-8"))):
        if isinstance(n, ast.ImportFrom) and n.module in ("md_eval.engine", "md_eval.signals", "md_eval.benchmarks", "md_eval.verdict"):
            problems.append(["P1", "research_gate imports an md_eval entry module", tree_mod])
sys.path.insert(0, str(Path(ROOT) / "tests"))
import i1_builders
fx = i1_builders.pb_v1_double()
before2, snap2 = set(sys.modules), state()
phase[0] = "P2"
code = None
with offline() as att2:
    try:
        research_gate.admit_double(fx)
    except research_gate.AdmissionRefused as exc:
        code = exc.code
phase[0] = None
delta2 = set(sys.modules) - before2
if code is not None:
    problems.append(["P2", "admission refused", code])
if att2:
    problems.append(["P2", "offline attempts", att2])
check("P2", True)
if state() != snap2:
    problems.append(["P2", "process state changed"])
new2 = {m.split(".")[0] for m in delta2 if m.split(".")[0] not in before2} - set(sys.stdlib_module_names)
if not new2 <= {"market_intel", "autorun", "setups", "tzdata"}:
    problems.append(["P2", "delta", sorted(new2)])
for m in ("market_intel.calendar", "autorun", "setups.market_hours"):
    if m not in sys.modules:
        problems.append(["P2", "not imported", m])
problems += [["P2", "refuse bound"] + x for x in refuse_bound(delta2)] + [["P2", "env read"] + x for x in env_reads(delta2)]
print(json.dumps({"problems": problems, "p1": len([e for e in events if e[0] == "P1"]), "p2": len([e for e in events if e[0] == "P2"])}))
'''


def _child(script, *args, bytecode=False, extra_env=None):
    env = {k: v for k, v in os.environ.items() if k not in ("PYTHONDONTWRITEBYTECODE", "PYTHONPYCACHEPREFIX")}
    env["STOCK_AGENT_SKIP_DOTENV"] = "1"
    if not bytecode:
        env["PYTHONDONTWRITEBYTECODE"] = "1"
    env.update(extra_env or {})
    cmd = [sys.executable] + ([] if bytecode else ["-B"]) + ["-c", script, str(ROOT.resolve()), *args]
    out = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True, env=env, timeout=600)
    assert out.returncode == 0, out.stderr[-3000:]
    return json.loads(out.stdout.strip().splitlines()[-1])


def test_O02_the_import_time_side_effects_of_the_gate_are_the_declared_ones():
    report = _child(O02_CHILD)
    assert report["problems"] == [] and report["p1"] > 0, json.dumps(report)[:6000]


def test_O03_a_swallowed_database_attempt_inside_admission_is_an_offline_violation(monkeypatch):
    real = CAL.crosscheck_cached

    def sneaky():
        try:
            sqlite3.connect(":memory:")
        except Exception:
            pass
        return real()
    monkeypatch.setattr(CAL, "crosscheck_cached", sneaky)
    with pytest.raises(RG.AdmissionRefused) as e:
        RG.admit_double(I1.pb_flat5())
    assert _code(e) == "OFFLINE_VIOLATION"


def test_O04_a_swallowed_out_of_bounds_read_inside_admission_is_refused(monkeypatch, tmp_path):
    outside = tmp_path / "outside.txt"
    outside.write_text("x", encoding="utf-8")
    real = CAL.tzdata_status

    def peeking():
        try:
            outside.read_text(encoding="utf-8")
        except PermissionError:
            pass
        return real()
    monkeypatch.setattr(CAL, "tzdata_status", peeking)
    with pytest.raises(RG.AdmissionRefused) as e:
        RG.admit(I1.pb_mda_v1_twin())
    assert _code(e) == "FILE_READ_NOT_ALLOWED"


O05_CHILD = r'''
import json, sys
from pathlib import Path
ROOT = sys.argv[1]
sys.path[:] = [ROOT if p == "" else p for p in sys.path]
import swing_pullback, swing_pullback.core, strategy_validation.dataset, strategy_validation.models, rotation_backtest.metrics
import md_eval, md_eval.contract, md_eval.double, md_eval.isolation
import research_gate
sys.path.insert(0, str(Path(ROOT) / "tests"))
import i1_builders
from historical_data import calendar as CAL
fx = i1_builders.pb_v1_double()
CAL.reset_caches()
code = None
try:
    research_gate.admit_double(fx)
except research_gate.AdmissionRefused as exc:
    code = exc.code
log = research_gate.read_log()
print(json.dumps({"code": code, "last": log[-1][0] if log else None, "pyc_write": any("PYC_WRITE" in str(e) for e in log)}))
'''


def test_O05_a_first_admission_writing_bytecode_has_no_guard_violation(tmp_path):
    prefix = tmp_path / "pyc2"
    prefix.mkdir()
    report = _child(O05_CHILD, bytecode=True, extra_env={"PYTHONPYCACHEPREFIX": str(prefix)})
    assert report == {"code": None, "last": "ADMIT_READ", "pyc_write": False}, report


def test_O06_a_missing_tzdata_package_refuses_the_warm_up(monkeypatch):
    monkeypatch.setattr(CAL, "_find_tzdata", lambda: None)
    CAL.reset_caches()
    try:
        with pytest.raises(RG.AdmissionRefused) as e:
            RG.admit_double(I1.pb_flat5())
        assert _code(e) == "CALENDAR_WARMUP_FAILED"
        assert e.value.reasons == ("TZDATA_UNAVAILABLE",), e.value.reasons          # S4-AM52: the sub-reason, not only the code
    finally:
        monkeypatch.undo()
        CAL.reset_caches()


# ============================================================================================================ md_eval's entry points (V)
def _ctx(view, session):
    return DecisionContext(view, session, BookSnapshot(session, 1e5, 1e5, False, ()))


def _five_sources():
    v1, c1 = SwingBridge(True, FX.UNIVERSE), SwingBridge(False, FX.UNIVERSE)
    return [v1, RandomEntry(v1, c1, 1), BuyAndHold("SIM:X"), EqualWeight(FX.UNIVERSE)]


def _assert_views_refused(view, session):
    for src in _five_sources():
        with pytest.raises(RG.NotAdmitted) as e:
            src.entries(_ctx(view, session))
        assert _code(e) == "NOT_ADMITTED_VIEW", type(src).__name__
    with pytest.raises(RG.NotAdmitted) as e:
        SwingBridge(True, FX.UNIVERSE).dataset(view, session)
    assert _code(e) == "NOT_ADMITTED_VIEW"


def test_V01_raw_double_views_and_raw_doubles_are_refused_by_the_sources_and_the_benchmarks():
    d = I1.pb_v1_double()
    s = d.sessions("0", "9")
    _assert_views_refused(d.as_of(d.session_times(s[210])[1]), s[210])
    for call in (lambda: BM.total_return_series(d, FX.SPY, s), lambda: BM.bound_series(d, FX.SPY, s),
                 lambda: BM.random_control(d, FX.v1_config(d), FX.UNIVERSE, [1])):
        with pytest.raises(RG.NotAdmitted) as e:
            call()
        assert _code(e) == "NOT_ADMITTED"


def test_V02_a_raw_md_a_view_is_refused_by_the_sources():
    p = I1.pb_mda_v1_twin()
    s = I1.twin_sessions()
    _assert_views_refused(p.as_of(CAL.close_utc(s[210])), s[210])


def test_V03_forged_views_are_refused():
    adm = RG.admit_double(I1.pb_flat5())
    s = adm.sessions("0", "9")
    issued = adm.as_of(adm.session_times(s[3])[1])
    RG.require_admitted_view(issued)

    class Sub(RG.AdmittedView):
        __slots__ = ()
        __eq__ = lambda a, b: True  # noqa: E731
        __hash__ = object.__hash__
    copies = []
    for cls in (RG.AdmittedView, Sub):
        f = object.__new__(cls)
        for slot in ("_inner", "_admission_sha256", "_flavour", "_universes"):
            object.__setattr__(f, slot, object.__getattribute__(issued, slot))
        copies.append(f)
    for forged in [object.__new__(RG.AdmittedView)] + copies:
        with pytest.raises(RG.NotAdmitted) as e:
            RG.require_admitted_view(forged)
        assert _code(e) == "NOT_ADMITTED_VIEW"
    for forged in copies:                                                       # the field-equal copies also reach a source
        with pytest.raises(RG.NotAdmitted) as e:
            BuyAndHold("SIM:X").entries(_ctx(forged, s[3]))
        assert _code(e) == "NOT_ADMITTED_VIEW"


def test_V04_the_verdict_refuses_an_identity_that_names_no_admission(tmp_path):
    import test_md_eval_registry_verdict as TRV
    w = TRV._world(tmp_path)
    res, _ = TRV._runs(w, "VALIDATION", 10, 29)
    for edit in (lambda a: None, lambda a: dict(a, admitted_as="HISTORICAL_SOMETHING"), lambda a: dict(a, admission_sha256="x" * 64)):
        bad = json.loads(json.dumps(res))
        for r in bad.values():
            r["identity"]["admission"] = edit(r["identity"]["admission"])
            r["identity_sha256"] = EN.sha256_of(r["identity"])
        with pytest.raises(VD.VerdictRefused, match="names no admission"):
            TRV._verdict(w, bad)


def _twin_world(tmp_path):
    import test_md_eval_registry_verdict as TRV
    adm = RG.admit(I1.pb_mda_v1_twin())
    s = adm.sessions("0", "9")
    spy = next(i.instrument_id for i in adm.instruments() if i.instrument_id.startswith("SYNTH:SPY:"))
    p = TRV._prereg(dataset_sha256=adm.manifest().content_sha256, segments={"TRAIN": [s[0], s[99]], "VALIDATION": [s[100], s[199]], "TEST": [s[200], s[259]]},
                    bootstrap={"mean_block_sessions": 5, "resamples": 200, "seed": 7, "confidence": 0.9}, benchmark_instrument=spy,
                    thresholds={"alpha": 0.10, "n_min_sessions": 10, "min_dsr": None}, embargo_sessions=0, initial_cash=10_000.0, cash_rate={"mode": "ZERO"})
    reg, led = RGY.TrialRegistry(tmp_path / "trials.jsonl"), RGY.HoldoutLedger(tmp_path / "holdout.jsonl")
    reg.reconstruct_priors()
    reg.register(p)
    led.record_prereg(p)
    return {"d": adm, "s": s, "p": p, "sha": PR.validate(p), "reg": reg, "led": led}


def test_V05_a_decision_cap_outside_md_evals_caps_still_caps_the_verdict(tmp_path):
    import test_md_eval_registry_verdict as TRV
    w = _twin_world(tmp_path)
    aapl = next(i.instrument_id for i in w["d"].instruments() if i.instrument_id.startswith("SYNTH:AAPL:"))
    c = w["d"].bars(aapl, w["s"][100], w["s"][100])[0].raw_close
    entries = {100: [Signal(aapl, raw_close=c, stop_raw=0.5 * c)]}
    kw = {"registry": w["reg"], "holdout": w["led"]}
    mk = lambda which, e: FX.ScriptedSource(w["s"], entries=e, role="STRATEGY", name=w["p"][which]["id"], identity_extra=TRV.ID_EXTRA)  # noqa: E731
    res = {"strategy": EN.run(w["d"], mk("strategy", entries), TRV._cfg(w, "VALIDATION", 100, 199), **kw),
           "strategy_2x": EN.run(w["d"], mk("strategy", entries), TRV._cfg(w, "VALIDATION", 100, 199, mult=2.0), **kw),
           "control": EN.run(w["d"], mk("control", {}), TRV._cfg(w, "VALIDATION", 100, 199), **kw)}
    out = TRV._verdict(w, res)
    assert "SURVIVORSHIP_BIAS_PRESENT" not in md_eval.VERDICT_CAPS and "SURVIVORSHIP_BIAS_PRESENT" in out["caps"]
    assert out["verdict"] == "NOT_ESTABLISHED" and out["reasons"][0]["code"] == "CAPPED"


# ============================================================================================================ V1 through the bridge (D)
def test_D01_v1_through_the_bridge_keeps_its_flags_and_the_frozen_hash(twin_run):
    _, r = twin_run
    assert {SP.FLAG_EARNINGS, "EARNINGS_DATA_UNAVAILABLE", "EXITS_NEXT_OPEN"} <= set(r["flags"])
    assert SP.preregistration_hash() == "6c20fdb4f9c093fed0a82c21c642dfceee2cc6d424104390c7c432e3cfa61903"
    assert r["identity"]["source"]["preregistration_hash"] == SP.preregistration_hash()
