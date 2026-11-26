"""
MD-B (the portfolio evaluation engine md_eval/): the freeze pins, as every stage since 6.8. S3: rebuilt on main after MD-A merged (PR #64)
from the reviewed commit 66deb51a.
  F01B  the change set since BASE_MDB (main after MD-A merged, PR #64) is exactly NEW_MDB + EDITED_MDB; nothing is deleted; no production file
        is edited (only tests and the CI manifest); no trading, broker, safety, data, research-data, historical-data, evaluator, strategy or
        page file changes; the CI manifest gains exactly the six new test files
  F02B  the reused packages (swing_pullback/, swing_eval/, strategy_validation/, rotation_backtest/) are byte-identical, so V1 / C1's frozen rules
        and hash are unchanged; md_eval/ and MD-B's unchanged test modules have the reviewed 66deb51a blob ids (REVIEWED_BLOBS_66DEB51A), and
        tests/test_md_eval_lookahead.py is that blob after its declared S3 delta (C01 reads the repository's contract copies, C04) is undone;
        no semantic, instrumentation or new-registration declaration
  F04B  every changed older test definition is declared and every bookkeeping edit is undone exactly
  F05B  the ledger changes only its docstring sentence and paragraph, its declarations, its helper and the re-hashed entries of the registered
        files MD-B edits; MD-A's paragraph, declarations and helpers are byte-identical to BASE_MDB
  F06B  design 7a: MD-A's freeze test reads MD-B's files at BASE_MDB; BASE_MDB is an ancestor of HEAD with MD-A's reviewed tree; MD-A's set is the
        whole change from BASE_MDA to BASE_MDB; the panel's, J-04's, the gate's and the recovery's freeze tests and the live security pins are
        not edited
"""
import ast
import hashlib
import json
import re
import subprocess
from pathlib import Path

import freeze_ledger as FL
import test_mda_freeze as TMA
import test_recovery_freeze as TR

ROOT = Path(__file__).resolve().parents[1]
SCRATCH = ("scratchpad/", "runtime/", "data/market_intel", "logs/", "browser_tests/artifacts/")
NEW_FILES_MDB = {"md_eval/__init__.py", "md_eval/benchmarks.py", "md_eval/book.py", "md_eval/cash_rate.py", "md_eval/contract.py", "md_eval/costs.py",
                 "md_eval/double.py", "md_eval/engine.py", "md_eval/inference.py", "md_eval/isolation.py", "md_eval/ledger.py", "md_eval/metrics.py",
                 "md_eval/prereg.py", "md_eval/prior_trials.py", "md_eval/registry.py", "md_eval/signals.py", "md_eval/verdict.py", "md_eval/view.py",
                 "tests/md_eval_fixtures.py", "tests/test_md_eval_benchmarks_stats.py", "tests/test_md_eval_book_costs.py", "tests/test_md_eval_ledger.py",
                 "tests/test_md_eval_lookahead.py", "tests/test_md_eval_registry_verdict.py", "tests/test_mdb_freeze.py"}
REVIEWED_MDA_TREE = "17f38d503dcf00e3d3db310a511b7e796aff65e4"                             # main after PR #64: MD-A merged with its reviewed tree
REVIEWED_BLOBS_66DEB51A = {                                                                 # S3-D3: git blob ids of the reviewed MD-B commit 66deb51a
    "md_eval/__init__.py": "e2b08d0ad87b89a092d67c19054a752f586b3458",
    "md_eval/benchmarks.py": "a02b6f10c373afbeb6361c20fa8002748a0f53fc",
    "md_eval/book.py": "82aba0f86e34456f843ac0ac5c7721fe216ab322",
    "md_eval/cash_rate.py": "9220d48b48a7b001422842b761120936624229fc",
    "md_eval/contract.py": "5bca0f12d4fa01ea0411bea679642c9de5ac0736",
    "md_eval/costs.py": "b0a1179f1a978af38ce15c85ce015d055709985f",
    "md_eval/double.py": "e1311f5f53059358416b6a75f473316ecd152d2b",
    "md_eval/engine.py": "39de399630782071d5d921da0418d18c996d770a",
    "md_eval/inference.py": "8baa0751caccfa05d388ae70b37fbc72d0fb41fc",
    "md_eval/isolation.py": "932abf9727dfadd7c864573a2b635b998cfcdd19",
    "md_eval/ledger.py": "fdded24faee64df5d3bf73139d877948cd4e5049",
    "md_eval/metrics.py": "206215b3f3fc3aa03eac12c8c4236b349b483e81",
    "md_eval/prereg.py": "ecb848164d5acf9327212dbcf27335df1c2a73fe",
    "md_eval/prior_trials.py": "dcf545debfda6324ffd879307e43f1bbbd37aea8",
    "md_eval/registry.py": "ca8204b15827b81e631c218978a716dadc01e987",
    "md_eval/signals.py": "c4f7cad06c54d1e6afffb78049ee5e05b3512931",
    "md_eval/verdict.py": "f2474c87279c7df1657ed1ccfae698d55c2d23b6",
    "md_eval/view.py": "ff29840967d0b91f1ce01e6530568ee34a768ad6",
    "tests/md_eval_fixtures.py": "8b06cef3801cbf813a0f64d610eef1153766b181",
    "tests/test_md_eval_benchmarks_stats.py": "ad54a2672cfa3af35580e5611d02be12207cf059",
    "tests/test_md_eval_book_costs.py": "597533ccc90836f5eafb92e17159135b4372b104",
    "tests/test_md_eval_ledger.py": "c14709bf24f0197ae869f904bc269afdebb6709d",
    "tests/test_md_eval_registry_verdict.py": "dc35db6917eb668df9731ee14ca73a117c6176be",
}
LOOKAHEAD_BLOB_66DEB51A = "c5d50e1c8a8487d6c96dc40ee240cfe300807bc7"                       # tests/test_md_eval_lookahead.py at 66deb51a
UNDO_LOOKAHEAD_S3 = [                                                                     # the declared S3 delta of tests/test_md_eval_lookahead.py: (66deb51a, S3)
    ("  C01-C03  contract identity: both contract files' sha256 (A12, local only), SUPPORTED_CONTRACTS = v2, any other version refused\n",
     "  C01-C04  contract identity: both contract files' sha256 (A12, the repository's copies), SUPPORTED_CONTRACTS = v2, any other version refused; md_eval's pins = historical_data's\n"),
    ('PLANNING = Path(r"C:\\Data Analyst Py\\DA IDX\\project-planning\\md")\n',
     'CONTRACT_DIR = Path(__file__).resolve().parents[1] / "historical_data" / "contract"   # S3 (I-1A section 7.3): the committed copies (MD-A, MDA-FR-6)\n'),
    ('    base, amend = PLANNING / "HISTORICAL_DATA_CONTRACT_v1.md", PLANNING / "HISTORICAL_DATA_CONTRACT_v2_AMENDMENT.md"\n'
     '    if not (base.is_file() and amend.is_file()):\n'
     '        pytest.skip("the contract files live outside the repository (local-only check, A12)")\n',
     '    base, amend = CONTRACT_DIR / "HISTORICAL_DATA_CONTRACT_v1.md", CONTRACT_DIR / "HISTORICAL_DATA_CONTRACT_v2_AMENDMENT.md"   # no skip: a missing copy fails\n'),
    ('    assert bad.calls == ["manifest"]\n',
     '    assert bad.calls == ["manifest"]\n\n\n'
     'def test_C04_md_evals_contract_pins_equal_historical_datas_and_the_identifiers_are_the_id10_strings():\n'
     '    import historical_data as HD\n'
     '    assert md_eval.CONTRACT_V1_SHA256 == HD.CONTRACT_BASE_SHA256\n'
     '    assert md_eval.CONTRACT_V2_SHA256 == HD.CONTRACT_SHA256\n'
     '    assert (md_eval.CONTRACT_V1, md_eval.CONTRACT_V2) == ("historical_data_contract_v1", "historical_data_contract_v2")   # the exact strings ID-10 exempts\n'),
]
REUSED_UNCHANGED = ("swing_pullback", "swing_eval", "strategy_validation", "rotation_backtest")
FORBIDDEN_PREFIX_MDB = ("execution/", "safety/", "paper/", "setups/", "proposals/", "trader_view/", "autorun/", "database/", "account_refresh/", "forward_validation/",
                        "forward_capture/", "market_intel/", "market_scan/", "scanner/", "dynamic_universe/", "data/", "research_data/", "scripts/", "agents/",
                        "browser_tests/", "rh_gateway/", "unified_portfolio/", "strategy_registry/", "explorer/", "cash_flow/", "api/", "frontend/",
                        "swing_pullback/", "swing_eval/", "strategy_validation/", "rotation_backtest/", "historical_data/")
OLDER_FREEZE_TESTS = ("tests/test_panel_freeze.py", "tests/test_j04_freeze.py", "tests/test_gate_freeze.py", "tests/test_recovery_freeze.py")   # S3-D2: not edited
LIVE_PINS = ("tests/test_ex06_local_gate.py", "tests/test_xss01_freeze.py", "tests/test_xss01_csp.py", "tests/frontend/xss01_app_js.test.cjs", "tests/test_stop_latch_read.py",
             "tests/frontend/stop_priority.test.cjs", "tests/test_stop_order_gate.py", "tests/frontend/stop_gate_copy.test.cjs", "tests/test_bot_position_recovery.py",
             "tests/frontend/recovery_activity.test.cjs", "tests/conftest.py", "config.py")
CI_NEW = {"core": ("test_mda_freeze.py", ["test_mdb_freeze.py"]),
          "research": ("test_historical_data_ms1_local_mda.py", ["test_md_eval_lookahead.py", "test_md_eval_ledger.py", "test_md_eval_book_costs.py",
                                                                 "test_md_eval_benchmarks_stats.py", "test_md_eval_registry_verdict.py"])}


def _git(*a):
    return subprocess.run(["git", *a], cwd=ROOT, capture_output=True, text=True, check=True).stdout


def at(commit, rel):
    return TR.at(commit, rel)


def at_base(rel):
    return at(FL.BASE_MDB, rel)


I1_MOVED = tuple(sorted(FL.EDITED_I1 - {"tests/test_mdb_freeze.py"}))   # I-1B-1 (design 7a): its edited files, read at BASE_I1 (tests/test_i1_freeze.py F06I)


def at_i1(rel):
    return at(FL.BASE_I1, rel)


def ledger_i1():
    """I-1B-1 (design 7a): the ledger at BASE_I1, the hashes MD-B's bookkeeping is checked against."""
    return FL.ledger_at(FL.BASE_I1)


EXEC1_MOVED = tuple(sorted(FL.EDITED_EXEC1 - {"tests/test_mdb_freeze.py"}))   # EXEC-P1 C1 (design 7a): its edited files, read at BASE_EXEC1


def at_exec1(rel):
    return at(FL.BASE_EXEC1, rel)


def now_text(rel):
    if rel in I1_MOVED:
        return at_i1(rel)
    if rel in EXEC1_MOVED:
        return at_exec1(rel)
    return (ROOT / rel).read_text(encoding="utf-8").replace("\r\n", "\n")


def changed_files(base):
    # I-1B-1 (design 7a): MD-B's side is the fixed diff from `base` to BASE_I1; the working tree's change set belongs to tests/test_i1_freeze.py
    return set(_git("diff", "--name-only", base, FL.BASE_I1).split())


def test_F01B_the_change_set_is_exactly_the_declared_files_and_no_production_file_changed():
    assert FL.BASE_MDB == "f46848e3b48e05c1d7c19dc35e1c083fc1e68802" and subprocess.run(["git", "cat-file", "-e", f"{FL.BASE_MDB}^{{commit}}"], cwd=ROOT).returncode == 0
    changed = changed_files(FL.BASE_MDB)
    assert changed == FL.NEW_MDB | FL.EDITED_MDB, {"unexpected": sorted(changed - FL.NEW_MDB - FL.EDITED_MDB), "missing": sorted((FL.NEW_MDB | FL.EDITED_MDB) - changed)}
    in_base = set(_git("ls-tree", "-r", "--name-only", FL.BASE_MDB).split())
    assert {p for p in changed if p not in in_base} == FL.NEW_MDB == NEW_FILES_MDB and {p for p in changed if p in in_base} == FL.EDITED_MDB
    assert not _git("diff", "--name-only", "--diff-filter=D", FL.BASE_MDB).split()
    assert all(p.startswith(("tests/", ".github/")) for p in FL.EDITED_MDB)                    # no production file is edited
    assert {p for p in FL.NEW_MDB if not p.startswith("tests/")} == {p for p in NEW_FILES_MDB if p.startswith("md_eval/")}
    assert not {p for p in changed if p.startswith(FORBIDDEN_PREFIX_MDB)}, sorted(p for p in changed if p.startswith(FORBIDDEN_PREFIX_MDB))
    groups, base_groups = json.loads(now_text(".github/ci/pytest_groups.json"))["groups"], json.loads(at_base(".github/ci/pytest_groups.json"))["groups"]
    assert {g: v for g, v in groups.items() if g not in CI_NEW} == {g: v for g, v in base_groups.items() if g not in CI_NEW}
    for g, (after, new) in CI_NEW.items():
        i = base_groups[g]["files"].index(after) + 1
        assert groups[g] == {**base_groups[g], "files": base_groups[g]["files"][:i] + new + base_groups[g]["files"][i:]}, g
    listed = {f for v in groups.values() for f in v["files"]}
    assert {p.split("/")[-1] for p in FL.NEW_MDB if p.startswith("tests/test_")} <= listed
    assert ledger_i1()[".github/ci/pytest_groups.json"] == FL.sha(now_text(".github/ci/pytest_groups.json"))   # I-1B-1: the manifest and its hash, both at BASE_I1


def test_F02B_the_reused_packages_and_the_reviewed_md_eval_blobs_are_byte_identical_and_nothing_semantic_is_declared():
    assert subprocess.run(["git", "diff", "--quiet", FL.BASE_MDB, "--", *REUSED_UNCHANGED, "config.py", "api", "safety", "data"], cwd=ROOT).returncode == 0
    assert subprocess.run(["git", "diff", "--quiet", FL.BASE_MDB, FL.BASE_EXEC1, "--", "execution"], cwd=ROOT).returncode == 0   # EXEC-P1 C1 (design 7a): execution/ as C1 found it
    assert set(_git("diff", "--name-only", FL.BASE_EXEC1, "--", "execution").split()) <= FL.NEW_EXEC1 | FL.EDITED_EXEC1          # ... and since then only C1's declared files
    assert not [p for p in _git("ls-files", "--others", "--exclude-standard", "--", *REUSED_UNCHANGED).split() if not p.startswith(SCRATCH)]
    import swing_pullback as SP
    assert SP.preregistration_hash() == "6c20fdb4f9c093fed0a82c21c642dfceee2cc6d424104390c7c432e3cfa61903"
    assert FL.SEMANTIC_MDB == {} and FL.INSTRUMENTATION_MDB == {} and FL.NEW_LEDGER_MDB == set()
    md = {p.relative_to(ROOT).as_posix() for p in (ROOT / "md_eval").rglob("*.py")}
    assert md == {p for p in REVIEWED_BLOBS_66DEB51A if p.startswith("md_eval/")} and len(md) == 18, sorted(md ^ {p for p in REVIEWED_BLOBS_66DEB51A if p.startswith("md_eval/")})
    assert set(REVIEWED_BLOBS_66DEB51A) | {"tests/test_md_eval_lookahead.py", "tests/test_mdb_freeze.py"} == FL.NEW_MDB
    for rel, blob in sorted(REVIEWED_BLOBS_66DEB51A.items()):
        assert (_git("rev-parse", f"{FL.BASE_I1}:{rel}") if rel in I1_MOVED else _git("hash-object", rel)).strip() == blob, rel                                     # the path (eol filter applies); no -w: nothing is written
    text = now_text("tests/test_md_eval_lookahead.py")
    for old, new in reversed(UNDO_LOOKAHEAD_S3):
        assert text.count(new) == 1, new[:80]
        text = text.replace(new, old)
    b = text.encode("utf-8")
    assert hashlib.sha1(b"blob %d\0" % len(b) + b).hexdigest() == LOOKAHEAD_BLOB_66DEB51A    # only the declared C01 / C04 delta


def test_F04B_every_changed_older_test_definition_is_declared_and_every_bookkeeping_edit_is_undone_exactly():
    actual = set()
    for rel in sorted(p for p in FL.EDITED_MDB if p.startswith("tests/") and p.endswith(".py") and p != "tests/freeze_ledger.py"):
        b, n = TR.module_defs(at_base(rel)), TR.module_defs(now_text(rel))
        assert set(b) <= set(n), (rel, sorted(set(b) - set(n)))
        actual |= {f"{rel}::{k}" for k in set(n) if b.get(k) != n[k]}
        assert TR.unnamed(FL.undo_mdb(rel, now_text(rel))) == TR.unnamed(at_base(rel)), rel
    declared = set(FL.SEMANTIC_TESTS_MDB)
    assert actual == declared, {"undeclared": sorted(actual - declared), "declared_but_unchanged": sorted(declared - actual)}
    assert all(re.match(r"^MDB-[A-Z]\d\d ", v) and len(v) > 60 for v in FL.SEMANTIC_TESTS_MDB.values())
    edited_tests = {p for p in FL.EDITED_MDB if p.startswith("tests/") and p.endswith((".py", ".cjs"))} - {"tests/freeze_ledger.py"}
    assert set(FL.UNDO_MDB) == edited_tests, sorted(set(FL.UNDO_MDB) ^ edited_tests)
    for rel in sorted(FL.UNDO_MDB):
        for old, new in FL.UNDO_MDB[rel]:
            assert now_text(rel).count(new) == 1 and at_base(rel).count(old) >= 1, (rel, new[:120])
        assert FL.undo_mdb(rel, now_text(rel)) == at_base(rel), rel                          # now -> f46848e3, byte for byte
    FL.UNDO_MDB["tests/_probe.py"] = [("y = 2\n", "y = 3\n")]
    try:
        assert FL.undo_mdb("tests/_probe.py", "x = 1\ny = 3\n") == "x = 1\ny = 2\n" and FL.undo_mdb("tests/other.py", "z\r\n") == "z\n"
    finally:
        del FL.UNDO_MDB["tests/_probe.py"]


def test_F05B_the_ledger_changes_only_its_declarations_its_helper_and_the_rehashed_entries_and_mdas_block_is_untouched():
    main_fl, fl = at_base("tests/freeze_ledger.py"), now_text("tests/freeze_ledger.py")
    led = lambda t: t[t.index("\nLEDGER = {\n") + 1:t.index("}\n", t.index("\nLEDGER = {\n")) + 2]  # noqa: E731
    main_ledger, ledger = ast.literal_eval(led(main_fl)[len("LEDGER = "):]), ast.literal_eval(led(fl)[len("LEDGER = "):])
    want = {k for k in main_ledger if k in FL.EDITED_MDB}
    assert want == {".github/ci/pytest_groups.json", "tests/test_strategy_validation_62.py"}
    bad = sorted((rel, FL.sha(now_text(rel))) for rel in want if ledger.get(rel) != FL.sha(now_text(rel)))
    assert not bad, "re-hash these LEDGER entries:\n" + "\n".join(f'    "{r}": "{h}",' for r, h in bad)
    for a in ("\nLEDGER = {\n", "\nBASE_MDA = ", "\nBASE_MDB = ", "\n\ndef unchanged(", "\n\ndef undo_rec(", "\n\n\ndef pre_mda(", "\n\ndef undo_mda(", "\n\ndef ledger_at(",
              "\n\n\ndef undo_mdb(", "\nMD-A (the historical data contract package", "\nMD-B (the portfolio evaluation engine",
              " + NEW_GATE + NEW_REC + NEW_MDA + NEW_MDB plus this ledger."):
        assert fl.count(a) == 1, a
    region = ast.parse(fl[fl.index("BASE_MDB = "):fl.index("\nLEDGER = {\n") + 1]).body
    assert [ast.unparse(t) for n in region for t in (n.targets if isinstance(n, ast.Assign) else [n.target])] == [
        "BASE_MDB", "NEW_MDB", "EDITED_MDB", "SEMANTIC_MDB", "INSTRUMENTATION_MDB", "NEW_LEDGER_MDB", "_U", "UNDO_MDB", "_FB", "SEMANTIC_TESTS_MDB"]
    assert set(ledger) == set(main_ledger)                                                      # no new registration
    rehashed = {k for k in main_ledger if main_ledger[k] != ledger[k]}
    assert rehashed == want, sorted(rehashed ^ want)
    for rel in rehashed:
        assert ledger[rel] == FL.sha(now_text(rel)) == ledger_i1()[rel], rel                     # I-1B-1: MD-B's hashes against its texts, both at BASE_I1
    pa, ma = fl.index("\nMD-A (the historical data contract package"), main_fl.index("\nMD-A (the historical data contract package")   # MD-A's region, as merged
    assert fl[pa:fl.index("\nMD-B (the portfolio evaluation engine")] == main_fl[ma:main_fl.index('"""', ma)]
    assert fl[fl.index("BASE_MDA = "):fl.index("BASE_MDB = ")] == main_fl[main_fl.index("BASE_MDA = "):main_fl.index("\nLEDGER = {\n") + 1]
    assert fl[fl.index("\n\n\ndef pre_mda("):fl.index("\n\n\ndef undo_mdb(")] == main_fl[main_fl.index("\n\n\ndef pre_mda("):main_fl.index("\n\ndef unchanged(")]
    start = fl.index("\nMD-B (the portfolio evaluation engine")
    rebuilt = fl.replace(fl[start:fl.index('"""', start)], "", 1)
    rebuilt = rebuilt.replace(rebuilt[rebuilt.index("BASE_MDB = "):rebuilt.index("\nLEDGER = {\n") + 1], "", 1)
    rebuilt = rebuilt.replace(rebuilt[rebuilt.index("\n\n\ndef undo_mdb(") + 1:rebuilt.index("\n\ndef unchanged(") + 1], "", 1)
    rebuilt = rebuilt.replace(led(rebuilt), led(main_fl), 1).replace(" + NEW_GATE + NEW_REC + NEW_MDA + NEW_MDB plus this ledger.", " + NEW_GATE + NEW_REC + NEW_MDA plus this ledger.", 1)
    assert rebuilt == main_fl                                                                   # the paragraph, the declarations, one helper and the ledger
    assert FL.ledger_at(FL.BASE_MDB) == main_ledger
    for rel in FL.LEDGER:
        assert FL.view(rel, FL.now_text(rel)) == FL.base_text(rel), rel                          # every registered file still reads as main (BASE_68) to the older freeze tests


def test_F06B_design_7a_mdas_freeze_test_reads_mdbs_files_at_a_fixed_commit_and_the_chain_is_unbroken():
    assert subprocess.run(["git", "merge-base", "--is-ancestor", FL.BASE_MDB, "HEAD"], cwd=ROOT).returncode == 0
    assert subprocess.run(["git", "merge-base", "--is-ancestor", FL.BASE_MDA, FL.BASE_MDB], cwd=ROOT).returncode == 0
    assert _git("rev-parse", f"{FL.BASE_MDB}^{{tree}}").strip() == REVIEWED_MDA_TREE
    mda = set(_git("diff", "--name-only", FL.BASE_MDA, FL.BASE_MDB).split())
    assert mda == FL.NEW_MDA | FL.EDITED_MDA, sorted(mda ^ (FL.NEW_MDA | FL.EDITED_MDA))          # MD-A as merged (PR #64, its reviewed tree)
    assert TMA.changed_files(FL.BASE_MDA) == mda
    assert set(TMA.MDB_MOVED) == FL.EDITED_MDB - {"tests/test_mda_freeze.py"} and len(TMA.MDB_MOVED) == len(set(TMA.MDB_MOVED))
    src = now_text("tests/test_mda_freeze.py")
    assert "def at_mdb(rel):\n    return at(FL.BASE_MDB, rel)\n" in src and "    if rel in MDB_MOVED:\n        return at_mdb(rel)\n" in src
    assert '    return set(_git("diff", "--name-only", base, FL.BASE_MDB).split())\n' in src and TMA.ledger_mdb() == FL.ledger_at(FL.BASE_MDB)
    for rel in ("tests/freeze_ledger.py", ".github/ci/pytest_groups.json", "tests/test_swing_pullback_78.py", "tests/test_historical_data_mda.py"):
        assert TMA.now_text(rel) == at_base(rel) != now_text(rel), rel                          # MD-A's freeze test reads MD-B's files as main left them
    for rel in OLDER_FREEZE_TESTS:
        assert now_text(rel) == at_base(rel), rel                                               # S3-D2: the four older freeze tests are byte-identical to main
    whole = changed_files(FL.BASE_MDA)
    in_mda_base = set(_git("ls-tree", "-r", "--name-only", FL.BASE_MDA).split())
    mdb = changed_files(FL.BASE_MDB)
    assert whole == (mda | mdb) - {p for p in mda & mdb if p in in_mda_base and now_text(p) == at(FL.BASE_MDA, p)}, sorted(whole ^ (mda | mdb))
    for rel in LIVE_PINS:
        assert now_text(rel) == at_base(rel), rel                                               # the live security pins are not edited
