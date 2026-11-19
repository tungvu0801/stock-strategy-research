"""
I-1B-1 (S4: the research admission gate): the freeze pins, as every stage since 6.8 (I-1A 5 I1-F01..F06; matrix I1-X01..X03).
  F01I  the change set since BASE_I1 (main after MD-B merged, PR #65) is exactly NEW_I1 + EDITED_I1, untracked and ignored files included
        (hidden_files: only a *.pyc directly in a __pycache__ directory and tests/.pytest_cache/ are skipped); nothing is deleted; the production
        edits are exactly the four md_eval files; no forbidden area; the CI manifest gains exactly the six new test files
  F02I  each md_eval edit is confined to its declared definitions plus one import, line by line (LINE_OPS_I1); every changed test definition is
        declared (SEMANTIC_TESTS_I1)
  F03I  every in-place edit of an older test file is undone exactly (UNDO_I1), and every undo chain undoes I-1B-1 innermost
  F04I  the ledger changes only its sentence, the I1 paragraph, the I1 declarations, undo_i1 and the two re-hashed entries; MD-A's and MD-B's
        blocks are byte-identical
  F05I  the frozen trees, the contract, the V1 hash, MD-B's unedited files and the PB-9 in-commit integrity pins (REVIEWED_BLOBS_I1) hold NOW
  F06I  design 7a: MD-B's freeze test reads I-1B-1's files at BASE_I1; MD-A's and the older freeze tests are not edited; the chain is unbroken
  X01I-X03I  the three research_gate/ isolation exemptions are directory-exact, catch a non-gate module, and every frozen isolation definition
        is byte-identical to BASE_I1
Stated limit (PB-9): a pinned file changed TOGETHER with its pin is not caught here (F04I checks the pins' format, never their values).
"""
import ast
import difflib
import importlib.util
import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

import freeze_ledger as FL
import test_mda_freeze as TMA
import test_mdb_freeze as TMB
import test_recovery_freeze as TR

ROOT = Path(__file__).resolve().parents[1]
SCRATCH = ("scratchpad/", "runtime/", "data/market_intel", "logs/", "browser_tests/artifacts/")
NEW_FILES_I1 = {"research_gate/__init__.py", "research_gate/admit.py", "research_gate/bridge.py", "research_gate/licence_evidence.py", "research_gate/snapshot.py",
                "research_gate/synthetic_allowlist.py", "tests/i1_builders.py", "tests/test_i1_admission.py", "tests/test_i1_bridge_conformance.py",
                "tests/test_i1_census.py", "tests/test_i1_freeze.py", "tests/test_i1_snapshot_toctou.py", "tests/test_i1_synthetic_boundary.py"}
PROD_I1 = {"md_eval/engine.py", "md_eval/signals.py", "md_eval/benchmarks.py", "md_eval/verdict.py"}
REVIEWED_MDB_TREE = "c051cc781ea8762e19dad75dbb4df40123e151f3"                              # main after PR #65: MD-B merged with its reviewed tree
CI_NEW_I1 = {"core": ("test_mdb_freeze.py", ["test_i1_freeze.py"]),
             "research": ("test_md_eval_registry_verdict.py", ["test_i1_admission.py", "test_i1_snapshot_toctou.py", "test_i1_synthetic_boundary.py",
                                                               "test_i1_bridge_conformance.py", "test_i1_census.py"])}
FORBIDDEN_PREFIX_I1 = TMB.FORBIDDEN_PREFIX_MDB + ("value_quality/", "scan_report/", "requirements.txt", "config.py", ".gitattributes")
DEFS_I1 = {"md_eval/engine.py": {"run", "_Run"}, "md_eval/signals.py": {"SwingBridge", "RandomEntry", "BuyAndHold", "EqualWeight"},
           "md_eval/benchmarks.py": {"total_return_series", "random_control"}, "md_eval/verdict.py": {"_bind", "_settings", "_decide"}}
METHODS_I1 = {("md_eval/engine.py", "_Run"): {"__init__", "_finish"}, ("md_eval/signals.py", "SwingBridge"): {"dataset", "entries"},
              ("md_eval/signals.py", "RandomEntry"): {"entries"}, ("md_eval/signals.py", "BuyAndHold"): {"entries"}, ("md_eval/signals.py", "EqualWeight"): {"entries"}}
NEW_IMPORTS_I1 = {"md_eval/engine.py": "import research_gate as RGATE", "md_eval/signals.py": "import research_gate as RGATE",
                  "md_eval/benchmarks.py": "import research_gate as RGATE", "md_eval/verdict.py": "from research_gate import ADMITTED_AS_REACHABLE"}
LINE_OPS_I1 = {                                                                            # file -> (main's removed lines, I-1B-1's added lines), in order
    'md_eval/benchmarks.py': (
        [],
        ['import research_gate as RGATE', '    RGATE.require_admitted(provider)                                     # I-1B-1 (E7): an admitted dataset only, before any data read', '    RGATE.require_admitted(provider)                                     # I-1B-1 (E7): after the licence and segment refusals']),
    'md_eval/engine.py': (
        ['def run(provider, source, cfg: RunConfig, *, registry=None, holdout=None) -> dict:', '    BK.check_frozen(cfg.book_overrides)', '    if cfg.delisting_mode not in DELISTING_MODES:', '        raise ValueError(f"delisting_mode must be one of {DELISTING_MODES}")', '    if source.role not in ("STRATEGY", "BENCHMARK"):', '        raise ValueError(f"unknown source role {source.role!r}")', '    m = provider.manifest()', '    check_version(m)', '    licence = licence_gate(m)                                                   # before any data is read', '    source_identity = source.identity()', '    source_name = str(source_identity.get("name", ""))', '    multiplier = float((cfg.cost_model or CostModel()).multiplier)', '    label, check, reg = segment_guard(m, cfg, registry, holdout, source_name, multiplier, source_identity, source.role)   # before any data is read', '    bench = (reg or {}).get("benchmark_instrument")', '    if bench is not None and bench not in {i.instrument_id for i in provider.instruments()}:', '        raise RunRefused(f"the pre-registered benchmark instrument {bench!r} is not in the dataset")', '    heads = {"registry_head_sha256": registry.head() if registry is not None else None,', '             "holdout_head_sha256": holdout.head() if holdout is not None else None}   # LOW-1: a later truncation is detectable', '    with offline() as attempts:', '        out = _Run(provider, source, cfg, m, licence, check, heads, bench).execute()', '                 benchmark_instrument: Optional[str] = None):', '                    "ratio_canonicalisation": None, "segment_check": self.segment_check, **self.heads, "benchmark": bench_id}'],
        ['import research_gate as RGATE', 'def run(provider, source, cfg: RunConfig, *, registry=None, holdout=None, admission=None) -> dict:', '    with offline() as attempts:                                                     # I-1B-1 (E6): the whole run, the pre-checks included', '        BK.check_frozen(cfg.book_overrides)', '        if cfg.delisting_mode not in DELISTING_MODES:', '            raise ValueError(f"delisting_mode must be one of {DELISTING_MODES}")', '        if source.role not in ("STRATEGY", "BENCHMARK"):', '            raise ValueError(f"unknown source role {source.role!r}")', '        m = RGATE.manifest_view(provider)                                           # the manifest only: no data read', '        check_version(m)', '        licence = licence_gate(m)                                                   # before any data is read', '        source_identity = source.identity()', '        source_name = str(source_identity.get("name", ""))', '        multiplier = float((cfg.cost_model or CostModel()).multiplier)', '        label, check, reg = segment_guard(m, cfg, registry, holdout, source_name, multiplier, source_identity, source.role)   # before any data is read', '        adm = RGATE.admit_for_engine(provider, admission=admission)                 # the admission gate: after the pre-data gates, before any data read', '        if RGATE.admitted_manifest(adm) != m:', '            raise RGATE.NotAdmitted(RGATE.ADMISSION_MISMATCH, (RGATE.ADMISSION_MISMATCH, "MANIFEST"))', '        bench = (reg or {}).get("benchmark_instrument")', '        if bench is not None and bench not in {i.instrument_id for i in adm.instruments()}:', '            raise RunRefused(f"the pre-registered benchmark instrument {bench!r} is not in the dataset")', '        heads = {"registry_head_sha256": registry.head() if registry is not None else None,', '                 "holdout_head_sha256": holdout.head() if holdout is not None else None}   # LOW-1: a later truncation is detectable', '        RGATE.verify(adm)                                                           # run start', '        out = _Run(adm, source, cfg, RGATE.admitted_manifest(adm), licence, check, heads, bench, admission=RGATE.identity_fields(adm)).execute()', '        RGATE.verify(adm)                                                           # run end: a change during the run refuses, nothing is recorded', '                 benchmark_instrument: Optional[str] = None, *, admission: Optional[dict] = None):', '        self.admission = dict(admission) if admission is not None else None          # I-1B-1: the admission identity (research_gate.identity_fields)', '        adm = self.admission or {}', '        flags |= set(adm.get("decision_flags") or ()) | set(adm.get("verdict_caps") or ())     # I-1B-1: the admission decision\'s flags and caps', '                    "ratio_canonicalisation": None, "segment_check": self.segment_check, **self.heads, "benchmark": bench_id,', '                    "admission": {k: adm[k] for k in ("admission_sha256", "admitted_as", "decision_sha256", "allowlist_entry_id", "verdict_caps")}', '                    if self.admission is not None else None}']),
    'md_eval/signals.py': (
        [],
        ['import research_gate as RGATE', '        RGATE.require_admitted_view(view)                                    # I-1B-1 (E7): only a view the admission gate issued', '        RGATE.require_admitted_view(ctx.view)                                # I-1B-1 (E7): only a view the admission gate issued', '        licence_gate(ctx.view.manifest())                                    # I-1B-1 (E7): the licence gate first, then the admitted view', '        RGATE.require_admitted_view(ctx.view)', '        RGATE.require_admitted_view(ctx.view)                                # I-1B-1 (E7)', '        RGATE.require_admitted_view(ctx.view)                                # I-1B-1 (E7)']),
    'md_eval/verdict.py': (
        ['            "kind": ident.get("kind"), "licence": ident.get("licence"), "benchmark": ident.get("benchmark")}', '    caps = sorted(set(flags) & set(VERDICT_CAPS))'],
        ['from research_gate import ADMITTED_AS_REACHABLE', '    adm = ident.get("admission")                                    # I-1B-1 (RR-2): a consistency check, not a gate', '    asha = adm.get("admission_sha256") if isinstance(adm, dict) else None', '    if not (isinstance(asha, str) and len(asha) == 64 and all(c in "0123456789abcdef" for c in asha) and adm.get("admitted_as") in ADMITTED_AS_REACHABLE):', '        raise VerdictRefused(f"{role}: the run identity names no admission (consistency check)")', '            "kind": ident.get("kind"), "licence": ident.get("licence"), "benchmark": ident.get("benchmark"),', '            "admission_sha256": (ident.get("admission") or {}).get("admission_sha256")}', '    caps = sorted((set(flags) & set(VERDICT_CAPS)) | {c for i in ids.values() for c in (i.get("admission") or {}).get("verdict_caps", [])})']),
}
FROZEN_LIVE_I1 = ("historical_data", "swing_pullback", "rotation_backtest", "strategy_validation", "swing_eval", "research_data", "value_quality", "market_intel",
                  "market_scan", "config.py", "requirements.txt", "api", "execution", "safety", "paper", "data")
REQUIRED_FROZEN_I1 = FROZEN_LIVE_I1[:5]
TREE_PINS_I1 = {"historical_data": "bac009620e83053b35c6b4f634bf6716b8e8305d", "swing_pullback": "7e3f3c63dcf1d070f3168353851a9b9750cb5425",
                "strategy_validation": "e46f18bc1c4921f5431ec80211ce4058b5e618fe", "swing_eval": "f1d866320bfd4d2617ed780155e402979b943524",
                "md_eval": "c76c126f3551e7328df98027147717695dba1a65", "rotation_backtest": "7a9d20dbd1610d928d53d254d03493483f143ff3"}
CONTRACT_BLOBS = {"historical_data/contract/HISTORICAL_DATA_CONTRACT_v1.md": "e12e81483aa57e6a26a949bd641197cfa50db2ae",
                  "historical_data/contract/HISTORICAL_DATA_CONTRACT_v2_AMENDMENT.md": "4b83f347f225820e8718819a3b55cc3e68e17b56",
                  "historical_data/contract/CONTRACT_SHA256.txt": "1080bbeddb5b9a803eb362f7cdc5967ea47bc0f1"}
LOOKAHEAD_BLOB_I1 = "19324a374a851af2461dd20dd320991d53839e62"                              # tests/test_md_eval_lookahead.py at BASE_I1 (not edited, Part A F-6)
MDA_FREEZE_BLOB = "bd3afe12023eea5d6a575090f1161cf9dd6b92af"                                # tests/test_mda_freeze.py at BASE_I1 (not edited, PB-2)
OLDER_FREEZE_TESTS_I1 = ("tests/test_mda_freeze.py", "tests/test_panel_freeze.py", "tests/test_j04_freeze.py", "tests/test_gate_freeze.py",
                         "tests/test_recovery_freeze.py", "tests/test_xss01_freeze.py")
ISO_FROZEN_I1 = (                                                                          # (file, definition name or a unique test-id prefix): byte-identical to BASE_I1
    ("tests/test_swing_pullback_78.py", "test_X01_"), ("tests/test_swing_pullback_78.py", "test_X02_"), ("tests/test_swing_pullback_78.py", "=ALLOWED_IMPORTS"),
    ("tests/test_sip_eval_78b1.py", "test_X01_"), ("tests/test_sip_eval_78b1.py", "test_X02_"), ("tests/test_sip_eval_78b1.py", "=FORBIDDEN"),
    ("tests/test_market_intel_70.py", "test_I01_"), ("tests/test_market_intel_store_71.py", "test_S01_"), ("tests/test_market_intel_sources_72.py", "test_S01_"),
    ("tests/test_market_intel_collector_73.py", "test_S01_"), ("tests/test_market_intel_quality_74.py", "test_X01_"),
    ("tests/test_market_intel_schema_v2_731.py", "test_X01_"), ("tests/test_md_eval_ledger.py", "test_L14_"), ("tests/test_historical_data_mda.py", "test_X01_"),
    ("tests/test_historical_data_mda.py", "test_X03_"), ("tests/test_historical_data_mda.py", "=ID10_LITERALS"), ("tests/test_historical_data_mda.py", "=STDLIB_OK"),
    ("tests/test_md_eval_registry_verdict.py", "test_X01_"), ("tests/test_strategy_validation_62.py", "test_F02_"), ("tests/test_strategy_validation_62.py", "=MARK_62"),
    ("tests/test_ex06_freeze.py", "test_F01X_"), ("tests/test_wave2_prc.py", "test_F01C_"), ("tests/test_unified_exposure_gate_691.py", "test_F03_"),
    ("tests/test_wave1_safety_remediation.py", "test_E03_"), ("tests/test_wave1_safety_remediation.py", "test_E04_"),
)
CHAIN_A, CHAIN_B = "FL.undo_mdb(rel, now_text(rel))", "FL.undo_mdb(rel, FL.undo_i1(rel, now_text(rel)))"
ACC_A = 'FL.undo_mdb("tests/test_forward_paper_610a.py", (ROOT / "tests" / "test_forward_paper_610a.py").read_text(encoding="utf-8"))'
ACC_B = 'FL.undo_mdb("tests/test_forward_paper_610a.py", FL.undo_i1("tests/test_forward_paper_610a.py", (ROOT / "tests" / "test_forward_paper_610a.py").read_text(encoding="utf-8")))'
HEX40, HEX64 = re.compile(r"^[0-9a-f]{40}$"), re.compile(r"^[0-9a-f]{64}$")


def _git(*a):
    return subprocess.run(["git", *a], cwd=ROOT, capture_output=True, text=True, check=True).stdout


def at(commit, rel):
    return TR.at(commit, rel)


def at_base(rel):
    return at(FL.BASE_I1, rel)


def now_text(rel):
    return (ROOT / rel).read_text(encoding="utf-8").replace("\r\n", "\n")


def changed_files(base):
    c = set(_git("diff", "--name-only", base).split()) | set(_git("ls-files", "--others", "--exclude-standard").split())
    return {p for p in c if not p.startswith(SCRATCH) and (ROOT / p).exists()}


def hidden_files(dirs):
    """Every file on disk under `dirs` that git neither tracks nor lists as untracked (an ignored file: logs/, runtime/, *.pyc ...), names only.
    Skipped: a *.pyc whose parent directory is __pycache__, and the subtree tests/.pytest_cache/ (pytest's rootdir cache)."""
    known = set(_git("ls-files", "--cached", "--", *dirs).splitlines()) | set(_git("ls-files", "--others", "--exclude-standard", "--", *dirs).splitlines())
    out = set()
    for d in dirs:
        for p in (ROOT / d).rglob("*"):
            if not p.is_file():
                continue
            rel = p.relative_to(ROOT).as_posix()
            if (p.suffix == ".pyc" and p.parent.name == "__pycache__") or rel.startswith("tests/.pytest_cache/"):
                continue
            if rel not in known:
                out.add(rel)
    return out


def line_ops(rel):
    base, now = at_base(rel).split("\n"), now_text(rel).split("\n")
    ops = [(i1, i2, j1, j2) for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(None, base, now, autojunk=False).get_opcodes() if tag != "equal"]
    return base, now, ops


def _ranges(text, names):
    tree = ast.parse(text)
    return [(n.lineno, n.end_lineno) for n in tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and n.name in names]


def _ci_helper():
    spec = importlib.util.spec_from_file_location("ci_pytest_groups", ROOT / "scripts" / "ci_pytest_groups.py")
    h = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(h)
    return h


# ============================================================================================================ F01I
def test_F01I_the_change_set_is_exactly_the_declared_files_and_nothing_is_hidden():
    assert FL.BASE_I1 == "f4eb67d732f6f07522751c3066b3ded45d875ba3" and subprocess.run(["git", "cat-file", "-e", f"{FL.BASE_I1}^{{commit}}"], cwd=ROOT).returncode == 0
    changed = changed_files(FL.BASE_I1)
    assert changed == FL.NEW_I1 | FL.EDITED_I1, {"unexpected": sorted(changed - FL.NEW_I1 - FL.EDITED_I1), "missing": sorted((FL.NEW_I1 | FL.EDITED_I1) - changed)}
    assert hidden_files(("research_gate", "md_eval", "historical_data", "tests")) == set()
    in_base = set(_git("ls-tree", "-r", "--name-only", FL.BASE_I1).split())
    assert {p for p in changed if p not in in_base} == FL.NEW_I1 == NEW_FILES_I1 and {p for p in changed if p in in_base} == FL.EDITED_I1
    assert len(FL.NEW_I1) == 13 and len(FL.EDITED_I1) == 37
    assert not _git("diff", "--name-only", "--diff-filter=D", FL.BASE_I1).split()
    assert {p for p in FL.EDITED_I1 if not p.startswith(("tests/", ".github/"))} == PROD_I1 == set(FL.SEMANTIC_I1)
    assert all(len(v) > 60 for v in FL.SEMANTIC_I1.values()) and FL.INSTRUMENTATION_I1 == {} and FL.NEW_LEDGER_I1 == set()
    assert {p for p in FL.EDITED_I1 if p.startswith(".github/")} == {".github/ci/pytest_groups.json"}
    rg = {p.relative_to(ROOT).as_posix() for p in (ROOT / "research_gate").rglob("*") if p.is_file() and not (p.suffix == ".pyc" and p.parent.name == "__pycache__")}
    assert rg == {p for p in FL.NEW_I1 if p.startswith("research_gate/")}, sorted(rg)
    assert not {p for p in changed if p.startswith(FORBIDDEN_PREFIX_I1)}, sorted(p for p in changed if p.startswith(FORBIDDEN_PREFIX_I1))
    groups, base_groups = json.loads(now_text(".github/ci/pytest_groups.json"))["groups"], json.loads(at_base(".github/ci/pytest_groups.json"))["groups"]
    assert json.loads(now_text(".github/ci/pytest_groups.json"))["_doc"] == json.loads(at_base(".github/ci/pytest_groups.json"))["_doc"]
    assert {g: v for g, v in groups.items() if g not in CI_NEW_I1} == {g: v for g, v in base_groups.items() if g not in CI_NEW_I1}
    for g, (after, new) in CI_NEW_I1.items():
        i = base_groups[g]["files"].index(after) + 1
        assert groups[g] == {**base_groups[g], "files": base_groups[g]["files"][:i] + new + base_groups[g]["files"][i:]}, g
    listed = {f for v in groups.values() for f in v["files"]}
    assert {p.split("/")[-1] for p in FL.NEW_I1 if p.startswith("tests/test_")} <= listed and "i1_builders.py" not in listed
    assert FL.LEDGER[".github/ci/pytest_groups.json"] == FL.sha(now_text(".github/ci/pytest_groups.json"))
    h = _ci_helper()
    assert h.problems(h.load()) == [] and sum(len(v["files"]) for v in h.load().values()) == 127 + 6 == len(h.discovered())


# ============================================================================================================ F02I
def test_F02I_the_md_eval_edits_are_confined_to_the_declared_definitions_lines_and_one_import():
    for rel in sorted(PROD_I1):
        base_t, now_t = at_base(rel), now_text(rel)
        b, n = TR.module_defs(base_t), TR.module_defs(now_t)
        assert set(b) == set(n), (rel, sorted(set(b) ^ set(n)))                                # no new or removed top-level name
        assert {k for k in b if b[k] != n[k]} == DEFS_I1[rel], (rel, sorted({k for k in b if b[k] != n[k]}))
        assert sorted(TR.unnamed(now_t)) == sorted(TR.unnamed(base_t) + [ast.dump(ast.parse(NEW_IMPORTS_I1[rel]).body[0])]), rel
        base, now, ops = line_ops(rel)
        removed, added = [l for i1, i2, _, _ in ops for l in base[i1:i2]], [l for _, _, j1, j2 in ops for l in now[j1:j2]]
        assert (removed, added) == LINE_OPS_I1[rel], (rel, removed, added)
        assert len(now) == len(base) + len(added) - len(removed), rel
        rb, rn = _ranges(base_t, DEFS_I1[rel]), _ranges(now_t, DEFS_I1[rel])
        for i1, i2, j1, j2 in ops:
            assert all(any(a <= i + 1 <= z for a, z in rb) for i in range(i1, i2)), (rel, "removed outside a declared definition", i1)
            assert all(any(a <= j + 1 <= z for a, z in rn) or now[j] == NEW_IMPORTS_I1[rel] for j in range(j1, j2)), (rel, "added outside a declared definition", j1)
        for word in ("historical_data", "research_data", "swing_eval", "market_intel", "localhost"):
            assert word not in now_t, (rel, word)
    assert "total_return" not in now_text("md_eval/engine.py")
    for (rel, cls), want in METHODS_I1.items():
        b, n = TR.class_defs(at_base(rel), cls), TR.class_defs(now_text(rel), cls)
        assert set(b) == set(n) and {k for k in b if b[k] != n[k]} == want, (rel, cls, sorted({k for k in b if b[k] != n.get(k)}))
    actual = set()
    for rel in sorted(p for p in FL.EDITED_I1 if p.startswith("tests/") and p.endswith(".py") and p != "tests/freeze_ledger.py"):
        b, n = TR.module_defs(at_base(rel)), TR.module_defs(now_text(rel))
        assert set(b) <= set(n), (rel, sorted(set(b) - set(n)))
        actual |= {f"{rel}::{k}" for k in set(n) if b.get(k) != n[k]}
    declared = set(FL.SEMANTIC_TESTS_I1)
    assert actual == declared, {"undeclared": sorted(actual - declared), "declared_but_unchanged": sorted(declared - actual)}
    assert len(declared) == 50 and all(re.match(r"^I1B-[A-Z]\d\d ", v) and len(v) > 60 for v in FL.SEMANTIC_TESTS_I1.values())


# ============================================================================================================ F03I
def test_F03I_every_in_place_edit_is_undone_exactly_and_every_chain_undoes_i1_innermost():
    edited_tests = {p for p in FL.EDITED_I1 if p.startswith("tests/") and p.endswith((".py", ".cjs"))} - {"tests/freeze_ledger.py"}
    assert set(FL.UNDO_I1) == edited_tests, sorted(set(FL.UNDO_I1) ^ edited_tests)
    for rel in sorted(FL.UNDO_I1):
        now, base = now_text(rel), at_base(rel)
        for old, new in FL.UNDO_I1[rel]:
            assert now.count(new) == 1 and base.count(old) >= 1, (rel, new[:120])
        assert FL.undo_i1(rel, now) == base, rel                                                # now -> f4eb67d7, byte for byte
        assert TR.unnamed(FL.undo_i1(rel, now)) == TR.unnamed(base), rel
    chains = [(rel, old, new) for rel, pairs in FL.UNDO_I1.items() for old, new in pairs if "FL.undo_i1(" in new]
    assert len(chains) == 8, len(chains)
    for rel, old, new in chains:
        assert new == (old.replace(ACC_A, ACC_B) if rel == "tests/test_wave2_pra_accounting.py" else old.replace(CHAIN_A, CHAIN_B)), rel
        assert old in {n for _, n in FL.UNDO_MDB[rel]}, rel                                    # the MD-B chain string, so the paren count is pinned
        assert old.count(CHAIN_A if rel != "tests/test_wave2_pra_accounting.py" else ACC_A) == 1, rel
    FL.UNDO_I1["tests/_probe.py"] = [("y = 2\n", "y = 3\n")]
    try:
        assert FL.undo_i1("tests/_probe.py", "x = 1\ny = 3\n") == "x = 1\ny = 2\n" and FL.undo_i1("tests/other.py", "z\r\n") == "z\n"
    finally:
        del FL.UNDO_I1["tests/_probe.py"]


# ============================================================================================================ F04I
REGION_NAMES_I1 = ["BASE_I1", "NEW_I1", "EDITED_I1", "SEMANTIC_I1", "INSTRUMENTATION_I1", "NEW_LEDGER_I1", "SYNTHETIC_ALLOWLIST_SHA256_I1", "REVIEWED_BLOBS_I1",
                   "_U_I1", "UNDO_I1", "_FI", "SEMANTIC_TESTS_I1"]


def test_F04I_the_ledger_changes_only_its_sentence_paragraph_declarations_helper_and_rehashed_entries():
    main_fl, fl = at_base("tests/freeze_ledger.py"), now_text("tests/freeze_ledger.py")
    led = lambda t: t[t.index("\nLEDGER = {\n") + 1:t.index("}\n", t.index("\nLEDGER = {\n")) + 2]  # noqa: E731
    main_ledger, ledger = ast.literal_eval(led(main_fl)[len("LEDGER = "):]), ast.literal_eval(led(fl)[len("LEDGER = "):])
    want = {k for k in main_ledger if k in FL.EDITED_I1}
    assert want == {".github/ci/pytest_groups.json", "tests/test_strategy_validation_62.py"}
    bad = sorted((rel, FL.sha(now_text(rel))) for rel in want if ledger.get(rel) != FL.sha(now_text(rel)))
    assert not bad, "re-hash these LEDGER entries:\n" + "\n".join(f'    "{r}": "{h}",' for r, h in bad)
    for a in ("\nLEDGER = {\n", "\nBASE_MDB = ", "\nBASE_I1 = ", "\n\ndef unchanged(", "\n\n\ndef undo_mdb(", "\n\n\ndef undo_i1(", "\nMD-B (the portfolio evaluation engine",
              "\nI-1B-1 (S4: the research admission gate", " + NEW_GATE + NEW_REC + NEW_MDA + NEW_MDB + NEW_I1 plus this ledger."):
        assert fl.count(a) == 1, a
    assert not [l for l in fl.split("\n") if l.startswith("I-1B-1 (") and not l.startswith("I-1B-1 (S4: the research admission gate")]
    region = ast.parse(fl[fl.index("BASE_I1 = "):fl.index("\nLEDGER = {\n") + 1]).body
    assert [ast.unparse(t) for n in region for t in (n.targets if isinstance(n, ast.Assign) else [n.target])] == REGION_NAMES_I1
    assert HEX64.match(FL.SYNTHETIC_ALLOWLIST_SHA256_I1) and all(HEX40.match(v) for v in FL.REVIEWED_BLOBS_I1.values())
    assert set(ledger) == set(main_ledger)                                                     # no new registration
    rehashed = {k for k in main_ledger if main_ledger[k] != ledger[k]}
    assert rehashed == want, sorted(rehashed ^ want)
    for rel in rehashed:
        assert ledger[rel] == FL.sha(now_text(rel)) == FL.LEDGER[rel], rel
    mb, i = main_fl.index("\nMD-B (the portfolio evaluation engine"), fl.index
    assert fl[i("\nMD-B (the portfolio evaluation engine"):i("\nI-1B-1 (S4: the research admission gate")] == main_fl[mb:main_fl.index('"""', mb)]
    assert fl[i("BASE_MDB = "):i("BASE_I1 = ")] == main_fl[main_fl.index("BASE_MDB = "):main_fl.index("\nLEDGER = {\n") + 1]
    assert fl[i("\n\n\ndef pre_mda("):i("\n\n\ndef undo_i1(")] == main_fl[main_fl.index("\n\n\ndef pre_mda("):main_fl.index("\n\ndef unchanged(")]
    start = fl.index("\nI-1B-1 (S4: the research admission gate")
    rebuilt = fl.replace(fl[start:fl.index('"""', start)], "", 1)
    rebuilt = rebuilt.replace(rebuilt[rebuilt.index("BASE_I1 = "):rebuilt.index("\nLEDGER = {\n") + 1], "", 1)
    rebuilt = rebuilt.replace(rebuilt[rebuilt.index("\n\n\ndef undo_i1(") + 1:rebuilt.index("\n\ndef unchanged(") + 1], "", 1)
    rebuilt = rebuilt.replace(led(rebuilt), led(main_fl), 1).replace(" + NEW_GATE + NEW_REC + NEW_MDA + NEW_MDB + NEW_I1 plus this ledger.",
                                                                     " + NEW_GATE + NEW_REC + NEW_MDA + NEW_MDB plus this ledger.", 1)
    assert rebuilt == main_fl                                                                  # the sentence, the paragraph, the declarations, one helper, the ledger
    assert FL.ledger_at(FL.BASE_I1) == main_ledger
    for rel in FL.LEDGER:
        assert FL.view(rel, FL.now_text(rel)) == FL.base_text(rel), rel                         # every registered file still reads as main (BASE_68)


# ============================================================================================================ F05I
def test_F05I_the_frozen_trees_the_contract_the_v1_hash_mdbs_unedited_files_and_the_reviewed_blobs_hold_now():
    assert subprocess.run(["git", "diff", "--quiet", FL.BASE_I1, "--", *FROZEN_LIVE_I1], cwd=ROOT).returncode == 0
    assert not [p for p in _git("ls-files", "--others", "--exclude-standard", "--", *REQUIRED_FROZEN_I1).split() if not p.startswith(SCRATCH)]
    assert hidden_files(REQUIRED_FROZEN_I1) == set()
    for tree, oid in TREE_PINS_I1.items():
        assert _git("rev-parse", f"{FL.BASE_I1}:{tree}").strip() == oid, tree
    import historical_data as HD
    import md_eval
    import swing_pullback as SP
    assert SP.preregistration_hash() == "6c20fdb4f9c093fed0a82c21c642dfceee2cc6d424104390c7c432e3cfa61903"
    assert HD.CONTRACT_BASE_SHA256 == "453676fd44554ac9f66ce2d168318893ea18d93916ea2eef3ffa0f8c4de40fd7" == md_eval.CONTRACT_V1_SHA256
    assert HD.CONTRACT_SHA256 == "7850580ba22195e92ee03f71e75d2d7eb3719176343e90f485ce47c3ac876ed8" == md_eval.CONTRACT_V2_SHA256
    for rel, blob in CONTRACT_BLOBS.items():
        assert _git("hash-object", rel).strip() == blob, rel
    md = {p.relative_to(ROOT).as_posix() for p in (ROOT / "md_eval").rglob("*.py")}
    assert len(md) == 18 and md == {p for p in TMB.REVIEWED_BLOBS_66DEB51A if p.startswith("md_eval/")}
    unedited = {p: b for p, b in TMB.REVIEWED_BLOBS_66DEB51A.items() if p not in FL.EDITED_I1}
    assert len(unedited) == 16 and "tests/md_eval_fixtures.py" in unedited and "tests/test_md_eval_book_costs.py" in unedited
    for rel, blob in sorted(unedited.items()):
        assert _git("hash-object", rel).strip() == blob, rel                                     # the reviewed 66deb51a blobs (no -w: nothing is written)
    assert _git("hash-object", "tests/test_md_eval_lookahead.py").strip() == LOOKAHEAD_BLOB_I1
    for rel in PROD_I1:
        assert _git("hash-object", rel).strip() != TMB.REVIEWED_BLOBS_66DEB51A[rel], rel
    assert FL.sha(now_text("research_gate/synthetic_allowlist.py")) == FL.SYNTHETIC_ALLOWLIST_SHA256_I1
    pinned = {p for p in FL.NEW_I1 if re.fullmatch(r"research_gate/[^/]+\.py", p)} | PROD_I1
    assert set(FL.REVIEWED_BLOBS_I1) == pinned and len(pinned) == 10, sorted(set(FL.REVIEWED_BLOBS_I1) ^ pinned)                  # PB-9 (b)
    assert all(HEX40.match(v) for v in FL.REVIEWED_BLOBS_I1.values())                                                             # PB-9 (c)
    for rel, blob in sorted(FL.REVIEWED_BLOBS_I1.items()):
        assert _git("hash-object", rel).strip() == blob, f"{rel}: not the reviewed blob (PB-9 (a))"                                # PB-9 (a)
    for rel in TMB.LIVE_PINS:
        assert now_text(rel) == at_base(rel), rel


# ============================================================================================================ F06I
def test_F06I_design_7a_mdbs_freeze_test_reads_i1s_files_at_a_fixed_commit_and_the_chain_is_unbroken():
    assert subprocess.run(["git", "merge-base", "--is-ancestor", FL.BASE_I1, "HEAD"], cwd=ROOT).returncode == 0
    assert subprocess.run(["git", "merge-base", "--is-ancestor", FL.BASE_MDB, FL.BASE_I1], cwd=ROOT).returncode == 0
    assert _git("rev-parse", f"{FL.BASE_I1}^{{tree}}").strip() == REVIEWED_MDB_TREE
    mdb = set(_git("diff", "--name-only", FL.BASE_MDB, FL.BASE_I1).split())
    assert mdb == FL.NEW_MDB | FL.EDITED_MDB, sorted(mdb ^ (FL.NEW_MDB | FL.EDITED_MDB))
    assert TMB.changed_files(FL.BASE_MDB) == mdb
    assert set(TMB.I1_MOVED) == FL.EDITED_I1 - {"tests/test_mdb_freeze.py"} and len(TMB.I1_MOVED) == len(set(TMB.I1_MOVED))
    src = now_text("tests/test_mdb_freeze.py")
    assert "def at_i1(rel):\n    return at(FL.BASE_I1, rel)\n" in src and "    if rel in I1_MOVED:\n        return at_i1(rel)\n" in src
    assert '    return set(_git("diff", "--name-only", base, FL.BASE_I1).split())\n' in src and TMB.ledger_i1() == FL.ledger_at(FL.BASE_I1)
    for rel in ("tests/freeze_ledger.py", ".github/ci/pytest_groups.json", "tests/test_market_intel_70.py", "md_eval/engine.py"):
        assert TMB.now_text(rel) == at_base(rel) != now_text(rel), rel
    for rel in OLDER_FREEZE_TESTS_I1:
        assert now_text(rel) == at_base(rel), rel
    assert _git("hash-object", "tests/test_mda_freeze.py").strip() == MDA_FREEZE_BLOB
    assert set(TMA.MDB_MOVED) == FL.EDITED_MDB - {"tests/test_mda_freeze.py"}
    whole, i1 = changed_files(FL.BASE_MDB), changed_files(FL.BASE_I1)
    in_mdb_base = set(_git("ls-tree", "-r", "--name-only", FL.BASE_MDB).split())
    assert whole == (mdb | i1) - {p for p in mdb & i1 if p in in_mdb_base and now_text(p) == at(FL.BASE_MDB, p)}, sorted(whole ^ (mdb | i1))


# ============================================================================================================ X01I-X03I
def _tree(tmp_path, monkeypatch):
    import test_historical_data_mda as THD
    import test_md_eval_registry_verdict as TRV
    r = tmp_path / "r"
    (r / "md_eval").mkdir(parents=True)
    shutil.copyfile(ROOT / "md_eval" / "__init__.py", r / "md_eval" / "__init__.py")          # MD-A X02's ID-10 file must exist and be read
    monkeypatch.setattr(THD, "ROOT", r)
    monkeypatch.setattr(TRV, "ROOT", r)
    return r, THD, TRV


def _put(r, rel, text):
    p = r / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")
    return p


def test_X01I_the_scans_still_catch_a_non_gate_module(tmp_path, monkeypatch):
    r, THD, TRV = _tree(tmp_path, monkeypatch)
    gate = _put(r, "research_gate/m.py", "import historical_data\n# reads md_eval\n")
    THD.test_X02_nothing_outside_the_package_and_the_tests_imports_it()
    TRV.test_X03_md_eval_never_names_the_evaluator_or_the_research_data_package_and_nothing_outside_imports_it()
    gate.unlink()
    other = _put(r, "other/m.py", "import historical_data\n")
    with pytest.raises(AssertionError):
        THD.test_X02_nothing_outside_the_package_and_the_tests_imports_it()
    other.write_text("# reads md_eval\n", encoding="utf-8")
    with pytest.raises(AssertionError):
        TRV.test_X03_md_eval_never_names_the_evaluator_or_the_research_data_package_and_nothing_outside_imports_it()


def test_X02I_the_exemptions_are_directory_exact(tmp_path, monkeypatch):
    r, THD, TRV = _tree(tmp_path, monkeypatch)
    shadow = _put(r, "research_gate_shadow/m.py", "import historical_data\n")
    with pytest.raises(AssertionError):
        THD.test_X02_nothing_outside_the_package_and_the_tests_imports_it()
    shadow.write_text("# reads md_eval\n", encoding="utf-8")
    with pytest.raises(AssertionError):
        TRV.test_X03_md_eval_never_names_the_evaluator_or_the_research_data_package_and_nothing_outside_imports_it()
    shadow.unlink()
    z = _put(r, "md_eval/z.py", "import research_gate_shadow\n")
    with pytest.raises(AssertionError):
        TRV.test_X02_md_eval_imports_only_the_standard_library_and_the_reused_modules()
    z.write_text("import research_gate.admit\n", encoding="utf-8")
    TRV.test_X02_md_eval_imports_only_the_standard_library_and_the_reused_modules()


def _def(defs, key):
    hits = [k for k in defs if k == key or (not key.startswith("=") and k.startswith(key))]
    assert len(hits) == 1, (key, hits)
    return hits[0]


def test_X03I_id10_and_every_frozen_isolation_definition_are_byte_identical_to_main():
    import test_historical_data_mda as THD
    assert THD.ID10_LITERALS == {"md_eval/__init__.py": ('"historical_data_contract_v1"', '"historical_data_contract_v2"')}
    rel = "tests/test_historical_data_mda.py"
    cut = lambda t: t[t.index("ID10_LITERALS = "):t.index("\n\n\n", t.index("def test_X02_nothing_outside_the_package_and_the_tests_imports_it"))]  # noqa: E731
    now, base = cut(now_text(rel)), cut(at_base(rel))
    assert cut(FL.undo_i1(rel, now_text(rel))) == base and now.replace('"research_gate/", ', "", 1) == base and now != base
    for rel, key in ISO_FROZEN_I1:
        b, n = TR.module_defs(at_base(rel)), TR.module_defs(now_text(rel))
        k = _def(b, key)
        assert n.get(k) == b[k], (rel, k)
