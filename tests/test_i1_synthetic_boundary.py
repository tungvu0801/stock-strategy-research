"""
I-1B-1 (S4): the synthetic boundary - the pinned allow-list and its trust procedure (matrix 4.12 P1-P9; Part A 6; Part C 4.3).
  S01-S07  generated real-looking data, relabelled MS-1-style bars, copied manifests, environment names, unpinned knobs and cuts, the caps
           and prefixes as tripwires: each refused with its exact code
  S08-S09  the table sha is pinned in the freeze ledger; the empty side-channel literal; the table is exactly what the builders generate
  S10-S19  builders under the read guard; revocation; table integrity; the side-channel rule; fingerprint completeness per field class;
           attack fingerprints absent; no production switch (S15, S15b); write-mode confinement and the nesting rule (S15c); the audit
           hook's process impact (S15d); the guard's documented limits (S15e, S15f, strict xfails); every builder at its declared step;
           origin strings; the lookup's kind; the builder lint
The read guard confines file opens and listings during admission and verify only: it is NOT a complete security sandbox (M-1; RR-1).
"""
import ast
import contextvars
import dataclasses
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import threading
from pathlib import Path

import pytest

import freeze_ledger as FL
import historical_data_fixtures as HF
import i1_builders as I1
import md_eval_fixtures as FX
import research_gate as RG
from historical_data import canonical as CN
from md_eval.contract import CorporateEvent, is_missing
RGA = sys.modules["research_gate.admit"]                                        # the module: the package attribute `admit` is the admit() function
from research_gate import snapshot as SN
from research_gate import synthetic_allowlist as SAL

ROOT = Path(__file__).resolve().parents[1]
HEX64 = re.compile(r"^[0-9a-f]{64}$")
FORBIDDEN_WORDS = ("research_data", "swing_eval", "swing_pullback", "market_intel")


def _code(e):
    return e.value.code


def _refused(fixture, code="SYNTHETIC_NOT_PINNED"):
    with pytest.raises(RG.AdmissionRefused) as e:
        I1.admit_any(fixture)
    assert _code(e) == code, (e.value.code, e.value.reasons)
    return e.value


# ============================================================================================================ S01-S07
def test_S01_a_generated_random_walk_is_refused_with_a_sha_only():
    closes = I1.lcg_walk(40)
    for fx in (I1.atk_s01_random_walk_double(), I1.atk_s01_random_walk_provider()):
        exc = _refused(fx)
        assert "LICENCE_EVIDENCE_UNDEFINED" in exc.reasons and HEX64.match(exc.fingerprint)
        assert not any(repr(c) in str(exc) for c in closes[:5])


def test_S02_relabelled_ms1_style_bars_and_a_network_sip_bundle_are_refused():
    _refused(I1.atk_s02_ms1_style_relabelled())
    _refused(I1.atk_s02_sip_network_bundle(), "SOURCE_UNREADABLE")


def test_S03_a_pinned_manifest_copied_onto_other_prices_is_refused():
    RG.admit_double(I1.pb_v1_double())
    _refused(I1.atk_s03_v1_manifest_on_other_prices())


def test_S04_environment_names_prove_nothing(monkeypatch):
    for k in ("PYTEST_CURRENT_TEST", "RESEARCH_GATE_SYNTHETIC", "SYNTHETIC_OK", "CI"):
        monkeypatch.setenv(k, "1")
    _refused(I1.atk_s01_random_walk_double())


def test_S05_an_unpinned_knob_variant_is_refused():
    RG.admit_double(I1.pb_v1_double())
    _refused(I1.atk_s05_v1_leak_known_at())


def test_S06_the_pinned_derivations_admit_and_an_unpinned_cut_is_refused():
    for b in (I1.pb_v1_truncate_200, I1.pb_v1_truncate_210, I1.pb_v1_truncate_240, I1.pb_v1_perturb_218):
        RG.admit_double(b())
    _refused(I1.atk_s06_v1_truncate_220())


CAPS_LITERAL = {
    "MDB_CONTRACT_DOUBLE": {"max_bars": 5000, "max_instruments": 25, "id_prefixes": ("SIM:",)},
    "MDA_SYNTHETIC_PROVIDER": {"max_bars": 5000, "max_instruments": 25, "id_prefixes": ("SYNTH:",)},
    "MDA_SIP_FIXTURE_BUNDLE": {"max_bars": 5000, "max_instruments": 25, "id_prefixes": ("ALPACA:",), "invented_symbols": ("QZXA", "QZXB", "QZXC"),
                               "network": False, "fixed_retrieved_at": "2025-03-31T22:00:00Z"},
}


def _guarded_with(monkeypatch, builder, *, edit=lambda r: r, caps=None):
    row = I1.generated_row(builder)
    monkeypatch.setattr(SAL, "ENTRIES", (edit(dict(row)),))
    if caps is not None:
        monkeypatch.setattr(SAL, "CAPS", caps)
    with pytest.raises(RG.AdmissionRefused) as e:
        I1.admit_any(builder())
    assert _code(e) == "ALLOWLIST_GUARD_FAILED"
    return e.value.reasons


def test_S07_the_caps_and_prefixes_are_tripwires_with_exact_sub_reasons(monkeypatch):
    assert SAL.CAPS == CAPS_LITERAL
    assert _guarded_with(monkeypatch, I1.atk_s07_over_cap) == ("CAP_EXCEEDED",)
    assert _guarded_with(monkeypatch, I1.atk_s07_real_prefix) == ("PREFIX_NOT_SYNTHETIC",)
    assert _guarded_with(monkeypatch, I1.pb_flat5, edit=lambda r: dict(r, bar_count=r["bar_count"] + 1)) == ("COUNTS_MISMATCH",)
    caps = {k: dict(v) for k, v in CAPS_LITERAL.items()}
    caps["MDA_SIP_FIXTURE_BUNDLE"]["invented_symbols"] = ("NOPE",)
    assert _guarded_with(monkeypatch, I1.pb_sip_invented_bundle, caps=caps) == ("SYMBOL_NOT_INVENTED",)


# ============================================================================================================ S08-S09
def test_S08_the_table_sha_is_pinned_in_the_freeze_ledger():
    assert FL.sha(FL.now_text("research_gate/synthetic_allowlist.py")) == FL.SYNTHETIC_ALLOWLIST_SHA256_I1


def test_S08b_the_empty_side_channel_literal_is_the_recomputed_sha():
    assert HEX64.match(SAL.EMPTY_SIDE_CHANNEL_SHA256)
    assert SAL.EMPTY_SIDE_CHANNEL_SHA256 == hashlib.sha256(CN.dumps({"adapter_notes": [], "dropped_symbols": []})).hexdigest()


def test_S09_regenerate(record_property):
    """The GENERATED fields of ENTRIES equal what the builders produce, as a set of rows; REVIEWED_FIELDS is keyed by exactly the pinned
    builders and each entry carries its reviewed fields. No skip path. On a mismatch the generated rows are printed (no data)."""
    rows = I1.generate_table()
    record_property("table_sha256", I1.table_sha256(rows))
    problems = []
    want, have = {I1.projection(r) for r in rows}, {I1.projection(e) for e in SAL.ENTRIES}
    if have != want:
        problems.append({"missing": len(want - have), "extra": len(have - want)})
    qual = {I1.qualified(b) for b, _ in I1.PINNED_BUILDERS}
    if set(SAL.REVIEWED_FIELDS) != qual:
        problems.append({"reviewed_fields_keys": sorted(set(SAL.REVIEWED_FIELDS) ^ qual)})
    for e in SAL.ENTRIES:
        rf = SAL.REVIEWED_FIELDS.get(e["origin"]["builder"])
        if rf is None or e["allowlist_entry_id"] != rf["allowlist_entry_id"] or tuple(e["origin"]["test_ids"]) != tuple(rf["test_ids"]):
            problems.append({"reviewed_fields": e["origin"]["builder"]})
    if problems:
        pytest.fail(f"{problems}\n{I1.entries_literal(rows)}", pytrace=False)


# ============================================================================================================ S10-S14
def test_S10_every_in_memory_builder_runs_under_the_read_guard_and_a_reading_builder_is_refused(tmp_path):
    RGA._warm_calendar()
    for b, _ in I1.PINNED_BUILDERS:
        if b is I1.pb_sip_invented_bundle:
            continue
        with RGA.read_guard() as g:
            fx = b()
            if b is I1.pb_flat5:
                I1.admit_any(fx)                                                # a root=None admission nests inside a read-only guard
        assert g.violations == [], (b.__name__, g.violations)
    bundle = I1.pb_sip_invented_bundle()                                        # its write phase: outside any outer guard
    assert sorted(p.relative_to(bundle.root).as_posix() for p in Path(bundle.root).rglob("*") if p.is_file()) == sorted(I1.sip_bundle_files())
    outside = tmp_path / "outside.txt"
    outside.write_text("x", encoding="utf-8")
    with pytest.raises(RG.AdmissionRefused) as e:
        RGA._guarded(None, lambda: outside.read_text(encoding="utf-8"))
    assert _code(e) == "FILE_READ_NOT_ALLOWED"


def test_S11_a_revoked_entry_refuses_its_fixture(monkeypatch):
    RG.admit_double(I1.pb_flat6())
    monkeypatch.setattr(SAL, "ENTRIES", tuple(e for e in SAL.ENTRIES if e["origin"]["builder"] != I1.qualified(I1.pb_flat6)))
    _refused(I1.pb_flat6())


def test_S12_the_table_check_reports_duplicates_reused_ids_and_counts_over_the_caps():
    assert RGA.allowlist_problems() == []
    base = dict(SAL.ENTRIES[0])
    rows = [base, dict(base, allowlist_entry_id="SAL-9998"), dict(base, fingerprint="a" * 64), dict(base, allowlist_entry_id="SAL-9997", fingerprint="b" * 64,
                                                                                                     bar_count=5001)]
    assert RGA.allowlist_problems(rows) == [("DUPLICATE_FINGERPRINT", "SAL-9998"), ("REUSED_ENTRY_ID", base["allowlist_entry_id"]),
                                            ("COUNT_OVER_CAP", "SAL-9997")]
    ids = [e["allowlist_entry_id"] for e in SAL.ENTRIES]
    assert len(ids) == len(set(ids)) and all(re.fullmatch(r"SAL-\d{4}", i) for i in ids)


def test_S12b_the_side_channel_rule():
    for e in SAL.ENTRIES:
        rf = SAL.REVIEWED_FIELDS[e["origin"]["builder"]]
        if e["kind_of_object"] == "MDB_CONTRACT_DOUBLE":
            assert e["side_channel_sha256"] is None, e["origin"]
        elif rf.get("side_channel_nonempty"):
            assert rf.get("side_channel_reason") and HEX64.match(e["side_channel_sha256"]) and e["side_channel_sha256"] != SAL.EMPTY_SIDE_CHANNEL_SHA256
        else:
            assert e["side_channel_sha256"] == SAL.EMPTY_SIDE_CHANNEL_SHA256, e["origin"]


def _variants(d):
    b0 = d._bars["SIM:X"]
    ins = d._instruments["SIM:X"]
    rep = lambda **kw: [dataclasses.replace(b0[1], **kw)] + list(b0[2:]) + [b0[0]]  # noqa: E731
    ev = CorporateEvent(event_id="E-NEW", instrument_id="SIM:X", type="CASH_DIVIDEND", effective_session=d._sessions[3], known_at=d._sessions[1] + "T21:00:00Z",
                        cash_amount=1.0)
    return {
        "ohlcv": d._clone(bars=rep(raw_close=b0[1].raw_close + 1.0, raw_high=b0[1].raw_high + 1.0)),
        "factors": d._clone(bars=rep(split_factor=2.0)),
        "known_at": d._clone(bars=rep(known_at=b0[1].session + "T22:00:00Z")),
        "instruments": d._clone(instruments=(dataclasses.replace(ins, sector="ENERGY"),)),
        "events": d._clone(events=(ev,)),
        "membership": d._clone(membership={FX.UNIVERSE: {"basis": "FIXED_CURRENT_UNIVERSE", "members": ()}}),
        "manifest_licence": d._clone(manifest=dataclasses.replace(d._manifest, licence=dict(d._manifest.licence, licence_ref="other"))),
        "manifest_dataset_id": d._clone(manifest=dataclasses.replace(d._manifest, dataset_id="md-b-other")),
        "knobs": d._clone(leak_known_at=True),
    }


def test_S13_every_field_class_is_in_the_fingerprint_and_the_side_channel_is_guarded():
    d = I1.pb_flat5()
    RG.admit_double(d)
    for name, v in _variants(d).items():
        assert SN.double_fingerprint(v) != SN.double_fingerprint(d), name
        _refused(v)
    ins = d._instruments["SIM:X"]                                               # F-14: a dict standing for a Missing encodes alike (documented)
    dict_for_missing = d._clone(instruments=(dataclasses.replace(ins, listing={"status": ins.listing.status, "reason": ins.listing.reason}),))
    assert SN.double_fingerprint(dict_for_missing) == SN.double_fingerprint(d)
    assert is_missing(ins.listing) and not is_missing(dict_for_missing._instruments["SIM:X"].listing)   # ...and changes behaviour: P4 checks it
    p = I1.pb_mda_lookahead_world()
    p._dropped = ("ZZQX",)
    exc = _refused(p, "ALLOWLIST_GUARD_FAILED")
    assert exc.reasons == ("SIDE_CHANNEL_MISMATCH",)
    q = I1.pb_mda_lookahead_world()
    q._notes = ({"removed_invalid": [["SYNTH:BBB:" + HF.S[0], HF.S[13]]]},)
    _refused(q, "SNAPSHOT_ROUNDTRIP_FAILED")


def _attack_fingerprint(b):
    fx = b()
    if isinstance(fx, I1.SourceBundle):
        return None
    if type(fx) is I1.ContractDouble:
        return SN.double_fingerprint(fx)
    return fx.manifest().content_sha256


def test_S14_no_attack_fingerprint_is_pinned_and_the_lists_are_disjoint():
    pinned = {e["fingerprint"] for e in SAL.ENTRIES}
    fps = [f for f in (_attack_fingerprint(b) for b in I1.ATTACK_BUILDERS) if f is not None]
    assert len(fps) >= 7 and not set(fps) & pinned
    assert not {b for b, _ in I1.PINNED_BUILDERS} & set(I1.ATTACK_BUILDERS)


# ============================================================================================================ S15 (no production switch)
def _rg_trees():
    return {f.name: ast.parse(f.read_text(encoding="utf-8")) for f in sorted((ROOT / "research_gate").glob("*.py"))}


def test_S15_no_environment_read_no_global_flag_no_setter_no_argv_and_exactly_one_sys_attribute():
    sys_assign = []
    for name, tree in _rg_trees().items():
        for n in ast.walk(tree):
            assert not (isinstance(n, ast.Attribute) and isinstance(n.value, ast.Name) and n.value.id == "os" and n.attr in ("environ", "getenv", "environb")), name
            assert not (isinstance(n, ast.Attribute) and isinstance(n.value, ast.Name) and n.value.id == "sys" and n.attr == "argv"), name
            assert not isinstance(n, (ast.Global, ast.Nonlocal)), name
            assert not (isinstance(n, ast.FunctionDef) and re.match(r"^(set|enable|disable|allow)_", n.name)), (name, n.name)
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == "setattr" and n.args and isinstance(n.args[0], ast.Name) \
                    and n.args[0].id == "sys":
                sys_assign.append((name, "setattr"))
            if isinstance(n, (ast.Assign, ast.AugAssign, ast.AnnAssign)):
                for t in (n.targets if isinstance(n, ast.Assign) else [n.target]):
                    if isinstance(t, ast.Attribute) and isinstance(t.value, ast.Name) and t.value.id == "sys":
                        sys_assign.append((name, t.attr))
    assert sys_assign == [("admit.py", "_research_gate_audit_hook")]


def _census_files():
    skip = ("tests/", ".venv/", "scratchpad/", ".claude/")
    for p in sorted(ROOT.rglob("*.py")):
        rel = p.relative_to(ROOT).as_posix()
        if not rel.startswith(skip) and "/site-packages/" not in rel:
            yield rel, p


def _allow_write_problems(rel, tree):
    out = []
    for c in ast.walk(tree):
        if not isinstance(c, ast.Call):
            continue
        for k in c.keywords:
            if k.arg == "allow_write" and not (isinstance(k.value, ast.Constant) and k.value.value is False):
                out.append((rel, c.lineno, "allow_write"))
        callee = c.func.attr if isinstance(c.func, ast.Attribute) else getattr(c.func, "id", "")
        if callee == "read_guard" and any(k.arg is None for k in c.keywords):
            out.append((rel, c.lineno, "**"))
    return out


def test_S15b_no_production_code_turns_write_mode_on():
    import inspect
    sig = inspect.signature(RGA.read_guard)
    assert str(sig) == "(root=None, *, allow_write=False)" and sig.parameters["allow_write"].kind is inspect.Parameter.KEYWORD_ONLY
    for pkg in ("research_gate", "md_eval"):
        for f in sorted((ROOT / pkg).glob("*.py")):
            assert _allow_write_problems(f.name, ast.parse(f.read_text(encoding="utf-8"))) == [], f.name
    bad = []
    for rel, p in _census_files():
        bad += _allow_write_problems(rel, ast.parse(p.read_text(encoding="utf-8", errors="replace")))
    assert bad == []


# ============================================================================================================ S15c (write mode and nesting)
def _outside_repo(tmp_path):
    assert not tmp_path.resolve().is_relative_to(ROOT.resolve()), "pytest tmp_path lies inside the repository; S15c needs a basetemp outside it"


def _fresh(tmp_path, name):
    d = tmp_path / name
    d.mkdir()
    return d


def test_S15c_i_a_write_inside_the_write_root_succeeds(tmp_path):
    _outside_repo(tmp_path)
    r = _fresh(tmp_path, "R")
    with RGA.read_guard(root=r, allow_write=True) as g:
        (r / "a.bin").write_bytes(b"x")
    assert g.violations == [] and (r / "a.bin").read_bytes() == b"x"


def test_S15c_ii_a_write_outside_the_write_root_is_refused(tmp_path):
    _outside_repo(tmp_path)
    r = _fresh(tmp_path, "R")
    with RGA.read_guard(root=r, allow_write=True) as g:
        with pytest.raises(PermissionError):
            (tmp_path / "outside.bin").write_bytes(b"x")
    assert len(g.violations) == 1 and not (tmp_path / "outside.bin").exists()


def test_S15c_iii_the_write_root_must_exist_lie_outside_the_repository_and_be_empty(tmp_path):
    _outside_repo(tmp_path)
    full = _fresh(tmp_path, "full")
    (full / "f").write_text("x", encoding="utf-8")
    for root, sub in ((None, "ROOT_MISSING"), (tmp_path / "absent", "ROOT_MISSING"), (ROOT / "tests", "ROOT_INSIDE_REPOSITORY"), (full, "ROOT_NOT_EMPTY")):
        with pytest.raises(RG.AdmissionRefused) as e:
            with RGA.read_guard(root=root, allow_write=True):
                pass
        assert (_code(e), e.value.reasons) == ("GUARD_ROOT_NOT_ALLOWED", (sub,)), root


def test_S15c_iv_write_mode_is_off_after_exit_and_a_nested_guard_restores_the_outer_state(tmp_path):
    _outside_repo(tmp_path)
    r = _fresh(tmp_path, "R")
    before = RGA._GUARD.get()
    with RGA.read_guard(root=r, allow_write=True):
        (r / "a.bin").write_bytes(b"x")
    assert RGA._GUARD.get() is before is None
    with RGA.read_guard(root=r) as g:
        with pytest.raises(PermissionError):
            (r / "b.bin").write_bytes(b"x")
    assert len(g.violations) == 1
    with RGA.read_guard(root=r) as outer:
        with RGA.read_guard() as inner:
            with pytest.raises(PermissionError):
                (r / "a.bin").read_bytes()
        assert inner.violations and RGA._GUARD.get() is outer
        assert (r / "a.bin").read_bytes() == b"x"
    assert outer.violations == []


def test_S15c_v_a_read_only_guard_refuses_a_write_inside_its_root(tmp_path):
    _outside_repo(tmp_path)
    r = _fresh(tmp_path, "R")
    (r / "a.bin").write_bytes(b"x")
    with RGA.read_guard(root=r) as g:
        assert (r / "a.bin").read_bytes() == b"x"
        with pytest.raises(PermissionError):
            (r / "c.bin").write_bytes(b"x")
    assert len(g.violations) == 1 and not (r / "c.bin").exists()


def test_S15c_vi_a_nested_write_mode_entry_with_a_root_is_refused(tmp_path):
    _outside_repo(tmp_path)
    r, r2 = _fresh(tmp_path, "R"), _fresh(tmp_path, "R2")
    with RGA.read_guard(root=r) as outer:
        with pytest.raises(RG.AdmissionRefused) as e:
            with RGA.read_guard(root=r2, allow_write=True):
                pass
        assert (_code(e), e.value.reasons) == ("GUARD_ROOT_NOT_ALLOWED", ("GUARD_NESTED",))
        assert RGA._GUARD.get() is outer and outer.violations == []
        with pytest.raises(PermissionError):
            (r2 / "x.bin").write_bytes(b"x")
    assert len(outer.violations) == 1


def test_S15c_vi_b_a_nested_write_mode_entry_without_a_root_is_refused_as_nested(tmp_path):
    _outside_repo(tmp_path)
    r = _fresh(tmp_path, "R")
    with RGA.read_guard(root=r) as outer:
        with pytest.raises(RG.AdmissionRefused) as e:
            with RGA.read_guard(root=None, allow_write=True):
                pass
        assert (_code(e), e.value.reasons) == ("GUARD_ROOT_NOT_ALLOWED", ("GUARD_NESTED",))
        assert RGA._GUARD.get() is outer and outer.violations == []


def test_S15c_vii_only_a_read_only_guard_without_a_root_may_nest_and_it_narrows(tmp_path):
    _outside_repo(tmp_path)
    r, r2 = _fresh(tmp_path, "R"), _fresh(tmp_path, "R2")
    (r / "a.bin").write_bytes(b"x")
    with RGA.read_guard(root=r) as outer:
        with pytest.raises(RG.AdmissionRefused) as e:
            with RGA.read_guard(root=r2):
                pass
        assert (_code(e), e.value.reasons) == ("GUARD_ROOT_NOT_ALLOWED", ("GUARD_NESTED",))
        assert RGA._GUARD.get() is outer and outer.violations == []
        with RGA.read_guard() as inner:
            with pytest.raises(PermissionError):
                (r / "a.bin").read_bytes()
        assert len(inner.violations) == 1 and (r / "a.bin").read_bytes() == b"x"


# ============================================================================================================ S15d (process impact)
def _child(script, *args):
    env = dict(os.environ, STOCK_AGENT_SKIP_DOTENV="1")
    out = subprocess.run([sys.executable, "-B", "-c", script, str(ROOT.resolve()), *args], cwd=ROOT, capture_output=True, text=True, env=env, timeout=300)
    assert out.returncode == 0, out.stderr[-3000:]
    return json.loads(out.stdout.strip().splitlines()[-1])


def test_S15d_e1_the_hook_is_inert_outside_a_guard(tmp_path):
    assert RGA._GUARD.get() is None
    (tmp_path / "w.bin").write_bytes(b"x")
    assert (tmp_path / "w.bin").read_bytes() == b"x" and (ROOT / "README.md").read_bytes()
    assert os.listdir(tmp_path) and list(os.scandir(tmp_path)) and os.listdir(ROOT) and list(os.scandir(ROOT))


E2_CHILD = r'''
import importlib, json, sys
sys.path[:] = [sys.argv[1] if p == "" else p for p in sys.path]
import research_gate
A = sys.modules["research_gate.admit"]
s1 = sys._research_gate_audit_hook
importlib.reload(A)
same = sys._research_gate_audit_hook is s1 and A._GUARD is s1[1]
refused = False
with A.read_guard() as g:
    try:
        open(sys.argv[2], "rb").read()
    except PermissionError:
        refused = True
print(json.dumps({"same": same, "refused": refused, "recorded": len(g.violations)}))
'''


def test_S15d_e2_the_hook_is_installed_once_per_process_even_after_a_reload(tmp_path):
    f = tmp_path / "outside.bin"
    f.write_bytes(b"x")
    assert _child(E2_CHILD, str(f)) == {"same": True, "refused": True, "recorded": 1}


E5_CHILD = r'''
import contextvars, importlib, json, sys
sys.path[:] = [sys.argv[1] if p == "" else p for p in sys.path]
out = {}
sys._research_gate_audit_hook = ("not-callable", None)
try:
    import research_gate
    out["malformed"] = "imported"
except ImportError:
    out["malformed"] = "ImportError"
out["left"] = "research_gate.admit" in sys.modules
del sys._research_gate_audit_hook
import research_gate
A = sys.modules["research_gate.admit"]
real = sys._research_gate_audit_hook[0]
sys._research_gate_audit_hook = (real, contextvars.ContextVar("other"))
try:
    importlib.reload(A)
    out["mismatched"] = "reloaded"
except ImportError:
    out["mismatched"] = "ImportError"
print(json.dumps(out))
'''


def test_S15d_e5_a_malformed_or_mismatched_sentinel_fails_the_import():
    assert _child(E5_CHILD) == {"malformed": "ImportError", "left": False, "mismatched": "ImportError"}


E6_CHILD = r'''
import json, sys
sys.path[:] = [sys.argv[1] if p == "" else p for p in sys.path]
def blocker(event, args):
    if event == "sys.addaudithook":
        raise RuntimeError("no new hooks")
sys.addaudithook(blocker)
try:
    import research_gate
    out = "imported"
except ImportError:
    out = "ImportError"
print(json.dumps({"import": out}))
'''


def test_S15d_e6_a_hook_installation_failure_fails_the_import():
    assert _child(E6_CHILD) == {"import": "ImportError"}


def test_S15d_e3_an_unexpected_argument_shape_outside_a_guard_raises_nothing():
    assert RGA._GUARD.get() is None
    sys.audit("open", 12345)
    sys.audit("open")
    sys.audit("os.listdir")
    sys.audit("os.scandir", object())


# ============================================================================================================ S15e, S15f (documented limits: strict xfails)
@pytest.mark.xfail(strict=True, reason="M-1 limitation: the guard's ContextVar state is not inherited by new threads (RR-1)")
def test_S15e_a_thread_started_inside_a_guard_is_confined(tmp_path):
    outside = tmp_path / "outside.bin"
    outside.write_bytes(b"x")
    r = _fresh(tmp_path, "R")
    seen = []

    def reader():
        try:
            outside.read_bytes()
            seen.append(None)
        except PermissionError as exc:
            seen.append(exc)
    with RGA.read_guard(root=r):
        t = threading.Thread(target=reader)
        t.start()
        t.join()
    assert isinstance(seen[0], PermissionError)


@pytest.mark.xfail(strict=True, reason="M-1 limitation: the guard confines opens and listings only; not a complete sandbox (RR-1)")
@pytest.mark.parametrize("op", ["remove", "rename", "mkdir", "stat"])
def test_S15f_operations_other_than_open_and_listing_are_confined(tmp_path, op):
    outside = _fresh(tmp_path, "outside")
    (outside / "a.bin").write_bytes(b"x")
    r = _fresh(tmp_path, "R")
    calls = {"remove": lambda: os.remove(outside / "a.bin"), "rename": lambda: os.rename(outside / "a.bin", outside / "b.bin"),
             "mkdir": lambda: os.mkdir(outside / "d"), "stat": lambda: os.stat(outside / "a.bin")}
    with RGA.read_guard(root=r):
        with pytest.raises(PermissionError):
            calls[op]()


# ============================================================================================================ S16-S19
def test_S16_every_pinned_builder_is_admitted_or_refused_exactly_at_its_declared_step():
    for b, expected in I1.PINNED_BUILDERS:
        try:
            I1.admit_any(b())
            got = None
        except RG.AdmissionRefused as exc:
            got = {"step": I1.STEP_OF_CODE.get(exc.code, 0), "code": exc.code}
        assert got == expected, (b.__name__, got)


def test_S17_no_origin_string_names_a_forbidden_package():
    for e in SAL.ENTRIES:
        text = json.dumps(e["origin"]) + json.dumps(SAL.REVIEWED_FIELDS.get(e["origin"]["builder"], {}))
        assert not [w for w in FORBIDDEN_WORDS if w in text], e["origin"]


def test_S18_a_provider_entry_never_admits_a_double_with_its_fingerprint(monkeypatch):
    d = I1.atk_s01_random_walk_double()
    fp = SN.double_fingerprint(d)
    row = {"allowlist_entry_id": "SAL-9999", "fingerprint": fp, "kind_of_object": "MDA_SYNTHETIC_PROVIDER", "instrument_count": 1, "bar_count": 40,
           "session_span": (d._sessions[0], d._sessions[-1]), "side_channel_sha256": None, "expected_fail_step": None, "origin": {"builder": "x", "test_ids": ()}}
    monkeypatch.setattr(SAL, "ENTRIES", (row,))
    _refused(d)


POW_ALLOWED_IN = {"_world_double"}                                              # declared: it reproduces test_md_eval_registry_verdict._world bit for bit


def test_S19_the_builders_use_no_platform_maths_randomness_clock_or_lazy_import():
    tree = ast.parse((ROOT / "tests" / "i1_builders.py").read_text(encoding="utf-8"))
    for fn in [n for n in tree.body if isinstance(n, ast.FunctionDef)]:
        for n in ast.walk(fn):
            assert not isinstance(n, (ast.Import, ast.ImportFrom)), fn.name                       # no import below module level (RC-08)
            if isinstance(n, ast.BinOp) and isinstance(n.op, ast.Pow):
                assert fn.name in POW_ALLOWED_IN, fn.name
    for n in ast.walk(tree):
        if isinstance(n, ast.Attribute) and isinstance(n.value, ast.Name):
            assert n.value.id not in ("math", "random", "time", "uuid", "secrets"), (n.value.id, n.attr)
            assert not (n.value.id == "datetime" and n.attr in ("now", "today", "utcnow")), n.attr
        if isinstance(n, (ast.Import, ast.ImportFrom)):
            names = [a.name for a in n.names] + ([n.module] if isinstance(n, ast.ImportFrom) and n.module else [])
            assert not {"math", "random", "time", "uuid", "secrets"} & set(names), names
