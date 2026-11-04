"""
Wave 2 PR B - the Stage 6.1 session classifier v2: its scope pins and freeze bookkeeping (WAVE2_PRB_DESIGN_REV3_4.md Revision 3.4.1 sections 7
and 8; WAVE2_PRB_TEST_PLAN_REV3_4.md section 3: T-W04B, T-W05B, F01B ... F03B), as every stage since 6.8 pins its own delta.

  T-W04B      the production diff since 5d6f7dd is exactly PR A's files plus PR B's five, and PR B's added lines carry no PR C token
  T-W05B      the closed list of functions PR B may change or add (design 7) against 50cb2d7b, and no new module-level statement
  F01B..F03B  PR B's change set, its declared test changes (SEMANTIC_TESTS_W2B) and every bookkeeping edit of an older test file, exactly
"""
import ast
import hashlib
import json
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
import freeze_ledger as FL  # noqa: E402
from test_wave2_pra import PR_A_FILES  # noqa: E402

sys.path.insert(0, str(ROOT))

PR_B_FILES = {"database/forward_validation_migrations.py", "forward_validation/store.py", "forward_validation/metrics.py", "forward_validation/service.py",
              "forward_capture/health.py"}                                                    # design 7: the five production files
PR_C_TOKENS_B = ("forward_runs", "forward_run_events", "bot_exposed", "bot_orders", "pa_cov", "paper_auto_cov")   # T-W04B (U-03: no eligibility in PR B)
N01_TOKENS = ("post_order", "insert_intent", "enable_auto", "import requests", "from requests", "robinhood", "anthropic", "paper-api.alpaca.markets", "alpaca_order_writer",
              "TradingClient", "update_intent", "update_position", "set_mode(", "apply_fill", "record_order(")   # V-18: Stage 6.1 N01's list
FN = "ForwardValidationService."
CLOSED_LIST_B = {                                                                                # design 7: file -> (may change, may add)
    "database/forward_validation_migrations.py": ({"=_STATEMENTS", "run_forward_validation_migrations"}, {"=V2_SAMPLE_COLUMNS", "=V2_SESSION_COLUMNS", "_add_columns"}),
    "forward_validation/store.py": ({"=SAMPLE_COLS", "=SESSION_MUTABLE"} | {"ForwardValidationStore." + n for n in ("_session", "add_sample", "insert_not_run", "open_session")},
                                    {"class EvidenceSourceAbsent"} | {"ForwardValidationStore." + n for n in ("query_strict", "samples_between", "own_tail", "samples_from_rowid",
                                                                                                             "max_at_before")}),
    "forward_validation/metrics.py": (set(), {"=G", "=H", "=K", "=CLASSIFIER_VERSION", "=EVIDENCE_READ_ATTEMPTS", "classify_session_v2", "hold_coverage", "session_gaps"}),
    "forward_validation/service.py": ({FN + n for n in ("_snapshot", "_record_session", "_finalize", "_session_observations", "_backfill_not_run")},
                                      {FN + n for n in ("_confirmed", "_suspects", "_clock_check", "_restart_times", "_settled", "_row_evidence", "_duplicate_evidence",
                                                        "_live_host_evidence", "_assess", "_account_days", "_evidence_read_failed")}),
    "forward_capture/health.py": ({"compute"}, {"_auto_disarm_exempt"}),
}
PINNED_B = [("forward_validation/service.py", FN + n) for n in ("observe", "_early_stop", "attention", "_detect_restart", "_previous_sample", "_live_host_attempts",
                                                                "_gate_violations", "order_reliability", "freshness", "broker_health", "cumulative", "daily_summary")] + \
           [("forward_validation/metrics.py", "classify_session")]                                # design 7: "pinned, notably"
SCRATCH = ("scratchpad/", "runtime/", "data/market_intel", "logs/", "browser_tests/artifacts/")


def _git(*a):
    return subprocess.run(["git", *a], cwd=ROOT, capture_output=True, text=True).stdout


def at_base(rel):
    run = subprocess.run(["git", "show", f"{FL.BASE_W2B}:{rel}"], cwd=ROOT, capture_output=True)
    assert run.returncode == 0, f"{rel} is not in {FL.BASE_W2B} (fetch-depth 0 is required)"
    return run.stdout.decode("utf-8").replace("\r\n", "\n")


def now_text(rel):
    return (ROOT / rel).read_text(encoding="utf-8").replace("\r\n", "\n")


def changed_files(base):
    c = set(_git("diff", "--name-only", base).split()) | set(_git("ls-files", "--others", "--exclude-standard").split())
    return {p for p in c if not p.startswith(SCRATCH) and (ROOT / p).exists()}


def named_nodes(text):
    """qualname -> AST dump: top-level functions, classes ('class NAME': bases, keywords, decorators and every non-method body statement),
    methods and '=NAME' for every module-level assignment."""
    out = {}
    for n in ast.parse(text).body:
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
            out[n.name] = ast.dump(n)
        elif isinstance(n, ast.ClassDef):
            shell = ast.ClassDef(name=n.name, bases=n.bases, keywords=n.keywords, decorator_list=n.decorator_list,
                                 body=[m for m in n.body if not isinstance(m, (ast.FunctionDef, ast.AsyncFunctionDef))])
            out["class " + n.name] = ast.dump(shell)
            for m in n.body:
                if isinstance(m, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    out[f"{n.name}.{m.name}"] = ast.dump(m)
        elif isinstance(n, (ast.Assign, ast.AnnAssign)) and all(isinstance(t, ast.Name) for t in (n.targets if isinstance(n, ast.Assign) else [n.target])):
            for t in (n.targets if isinstance(n, ast.Assign) else [n.target]):
                out["=" + t.id] = ast.dump(n)
    return out


def unnamed(text):
    """Every module-level statement named_nodes() does not hold (imports, expressions, attribute or tuple assignments); not the docstring."""
    tree = ast.parse(text)
    body = tree.body[1:] if tree.body and isinstance(tree.body[0], ast.Expr) and isinstance(getattr(tree.body[0], "value", None), ast.Constant) else tree.body
    return [ast.dump(n) for n in body if not (isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) or (
        isinstance(n, (ast.Assign, ast.AnnAssign)) and all(isinstance(t, ast.Name) for t in (n.targets if isinstance(n, ast.Assign) else [n.target]))))]


def digest(dump: str) -> str:
    return hashlib.sha256(dump.encode()).hexdigest()


def added_lines(rel):
    base = set(at_base(rel).splitlines())
    return "\n".join(l for l in now_text(rel).splitlines() if l not in base)


# ================================================================ T-W04B, T-W05B: the allow-list and the closed function list ================

def test_TW04B_the_production_diff_is_pr_a_plus_pr_b_and_pr_b_adds_no_pr_c_or_order_token():
    prod = {p for p in changed_files(FL.BASE_W2A) if not p.startswith("tests/") and p != ".github/ci/pytest_groups.json"}
    assert prod == PR_A_FILES | PR_B_FILES, {"unexpected": sorted(prod - PR_A_FILES - PR_B_FILES), "missing": sorted((PR_A_FILES | PR_B_FILES) - prod)}
    mine = {p for p in changed_files(FL.BASE_W2B) if not p.startswith("tests/") and p != ".github/ci/pytest_groups.json"}
    assert mine == PR_B_FILES, sorted(mine ^ PR_B_FILES)
    for rel in sorted(PR_B_FILES):
        added = added_lines(rel)
        assert added and not [t for t in PR_C_TOKENS_B if t in added], rel
        assert not [t for t in N01_TOKENS if t in added] and "disable_auto(" not in added, rel   # V-18: the merged bans, re-run on PR B's lines
        assert not re.search(r"\brequests\b|robinhood|update_intent|set_mode\(|# >>> Stage 6\.1", added), rel   # 6.10A F05's list (health.py is a 6.10A file)
    from database.forward_validation_migrations import V2_SAMPLE_COLUMNS, V2_SESSION_COLUMNS
    from forward_validation.store import SAMPLE_COLS
    assert not {"run_id", "frozen_hash"} & ({c for c, _ in V2_SAMPLE_COLUMNS} | set(SAMPLE_COLS) | {c for c, _ in V2_SESSION_COLUMNS})
    assert not {p for p in mine if p.startswith(("safety/", "cash_flow/", "autorun/", "execution/", "setups/", "proposals/", "frontend/", "strategy_registry/"))}
    assert "config.py" not in changed_files(FL.BASE_W2B) and {p for p in mine if p.startswith("forward_capture/")} == {"forward_capture/health.py"}


def test_TW05B_only_the_listed_functions_changed_and_only_the_listed_names_are_new():
    for rel, (may_change, may_add) in CLOSED_LIST_B.items():
        now, base = named_nodes(now_text(rel)), named_nodes(at_base(rel))
        assert set(base) <= set(now), (rel, sorted(set(base) - set(now)))                          # nothing of 50cb2d7b disappears
        assert set(now) - set(base) == may_add, (rel, sorted((set(now) - set(base)) ^ may_add))     # exactly the listed names are new
        changed = {k for k in base if digest(base[k]) != digest(now[k])}
        assert changed <= may_change, (rel, sorted(changed - may_change))                           # every other function hash-equal
        assert sorted(unnamed(now_text(rel))) == sorted(unnamed(at_base(rel))), rel                 # no new module-level statement (imports go inside)
    store_class = ast.parse(now_text("forward_validation/store.py")).body
    cls = next(n for n in store_class if isinstance(n, ast.ClassDef) and n.name == "EvidenceSourceAbsent")
    assert [ast.unparse(b) for b in cls.bases] == ["ForwardValidationStoreError"] and not cls.keywords
    for rel, name in PINNED_B:
        assert digest(named_nodes(now_text(rel))[name]) == digest(named_nodes(at_base(rel))[name]), (rel, name)
    probe = named_nodes("class A(B):\n    x = 1\n    def f(self):\n        pass\n")
    assert set(probe) == {"class A", "A.f"} and named_nodes("class A(C):\n    x = 1\n")["class A"] != probe["class A"]   # bases and body are seen
    assert len(unnamed('"""doc"""\nimport os\nx = 1\nA.f = 2\n')) == 2


# ================================================================ F: scope and bookkeeping (as every stage since 6.8) =========================

def test_F01B_the_change_set_is_exactly_the_declared_files_and_no_strategy_risk_broker_or_market_data_file_changed():
    assert FL.BASE_W2B == "50cb2d7bc0384f1a020c01c9ba22c52c1283f339" and subprocess.run(["git", "cat-file", "-e", f"{FL.BASE_W2B}^{{commit}}"], cwd=ROOT).returncode == 0
    changed = changed_files(FL.BASE_W2B)
    assert changed == FL.NEW_W2B | FL.EDITED_W2B, {"unexpected": sorted(changed - FL.NEW_W2B - FL.EDITED_W2B), "missing": sorted((FL.NEW_W2B | FL.EDITED_W2B) - changed)}
    in_base = set(_git("ls-tree", "-r", "--name-only", FL.BASE_W2B).split())
    assert {p for p in changed if p not in in_base} == FL.NEW_W2B and {p for p in changed if p in in_base} == FL.EDITED_W2B
    assert not _git("diff", "--name-only", "--diff-filter=D", FL.BASE_W2B).split()                 # nothing of 50cb2d7b is deleted
    app = {p for p in FL.EDITED_W2B | FL.NEW_W2B if not p.startswith("tests/")}
    assert app == PR_B_FILES | {".github/ci/pytest_groups.json"} and set(FL.SEMANTIC_W2B) == PR_B_FILES and FL.INSTRUMENTATION_W2B == {}
    assert all(len(v) > 40 for v in FL.SEMANTIC_W2B.values())
    marker = re.compile(r"^\s*(#|//|<!--)\s*(>>>|<<<) ")                                       # a marked block an older freeze test would strip unseen
    for rel in sorted(changed):
        base = set(at_base(rel).splitlines()) if rel in in_base else set()
        assert not [l for l in now_text(rel).splitlines() if l not in base and marker.match(l)], rel
    groups, base_groups = json.loads(now_text(".github/ci/pytest_groups.json"))["groups"], json.loads(at_base(".github/ci/pytest_groups.json"))["groups"]
    assert {g: v for g, v in groups.items() if g != "execution"} == {g: v for g, v in base_groups.items() if g != "execution"}   # V-17: one group gains them
    assert groups["execution"] == {**base_groups["execution"], "files": base_groups["execution"]["files"] + ["test_wave2_prb.py", "test_wave2_prb_sessions.py"]}


def defs(text):
    """name -> AST dump of every top-level function, class and plain-name assignment of a test file (F02B's unit)."""
    out = {}
    for n in ast.parse(text).body:
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            out[n.name] = ast.dump(n)
        elif isinstance(n, (ast.Assign, ast.AnnAssign)):
            for t in (n.targets if isinstance(n, ast.Assign) else [n.target]):
                if isinstance(t, ast.Name):
                    out["=" + t.id] = ast.dump(n)
    return out


def test_F02B_every_changed_test_function_is_declared_and_no_order_or_mode_call_was_added():
    actual = set()
    for rel in sorted(p for p in FL.EDITED_W2B if p.startswith("tests/") and p.endswith(".py") and p != "tests/freeze_ledger.py"):   # the ledger: F03B
        b, n = defs(at_base(rel)), defs(now_text(rel))
        assert set(b) <= set(n), (rel, sorted(set(b) - set(n)))                                   # no merged test removed
        actual |= {f"{rel}::{k}" for k in set(n) if b.get(k) != n[k]}
    declared = set(FL.SEMANTIC_TESTS_W2B)
    assert actual == declared, {"undeclared": sorted(actual - declared), "declared_but_unchanged": sorted(declared - actual)}
    assert all(re.match(r"^(U|V|W)-\d\d", v) and len(v) > 40 for v in FL.SEMANTIC_TESTS_W2B.values())   # each entry names its finding
    out = subprocess.run([sys.executable, "-c", "import forward_validation as FV; print(FV.frozen_hash())"], cwd=ROOT, capture_output=True, text=True)   # a clean process
    assert out.stdout.split() == ["b5d4a56ef5bba61f8250b3cd25182cc1655738adf0605a77671fd029883f8530"], out.stderr[-600:]
    assert now_text("forward_validation/service.py").count("disable_auto(") == at_base("forward_validation/service.py").count("disable_auto(") == 1
    for rel in FL.SEMANTIC_W2B:
        assert FL.pre_w2b(rel, now_text(rel)) == at_base(rel) and now_text(rel) != at_base(rel), rel   # what older freeze tests read: main's text, exactly
    assert FL.pre_w2b("config.py", "x") == "x" and now_text("forward_validation/__init__.py") == at_base("forward_validation/__init__.py")


def test_F03B_every_bookkeeping_edit_is_pinned_exactly_and_the_ledger_changes_only_the_declared_entries():
    assert set(FL.UNDO_W2B) == {p for p in FL.EDITED_W2B if p.startswith("tests/") and p.endswith(".py")} - {"tests/freeze_ledger.py"}
    for rel in sorted(FL.UNDO_W2B):
        assert FL.undo_w2b(rel, now_text(rel)) == at_base(rel), rel                             # now -> 50cb2d7b, byte for byte
    main_fl, fl = at_base("tests/freeze_ledger.py"), now_text("tests/freeze_ledger.py")
    assert fl.count("LEDGER = {") == 1 and fl.count("BASE_W2B = ") == 1                       # the anchors every older ledger test uses stay unique
    led = lambda t: t[t.index("\nLEDGER = {\n") + 1:t.index("}\n", t.index("\nLEDGER = {\n")) + 2]  # noqa: E731
    main_ledger = ast.literal_eval(led(main_fl)[len("LEDGER = "):])
    start = fl.index("\nWave 2 PR B (the Stage 6.1 session classifier v2")
    rebuilt = fl.replace(fl[start:fl.index('"""', start)], "", 1).replace(" + NEW_W2A + NEW_W2B plus this ledger.", " + NEW_W2A plus this ledger.", 1)
    rebuilt = rebuilt.replace(rebuilt[rebuilt.index("BASE_W2B = "):rebuilt.index("\nLEDGER = {\n") + 1], "", 1)
    rebuilt = rebuilt.replace(rebuilt[rebuilt.index("\n\ndef pre_w2b("):rebuilt.index("\n\ndef unchanged(")], "", 1)
    rebuilt = rebuilt.replace(led(rebuilt), led(main_fl), 1)
    assert rebuilt == main_fl                                                                 # the paragraph, one line, the declarations, two helpers and the ledger
    assert set(FL.LEDGER) - set(main_ledger) == FL.NEW_LEDGER_W2B and set(main_ledger) <= set(FL.LEDGER)
    assert {k for k in main_ledger if main_ledger[k] != FL.LEDGER[k]} == FL.EDITED_W2B & set(main_ledger)   # every re-hash is a declared PR B edit
    for rel in sorted(FL.NEW_LEDGER_W2B):
        assert FL.base_text(rel) == at_base(rel) == FL.pre_w2b(rel, now_text(rel)), rel       # unchanged from BASE_68 to main: the ledger view IS main's text
    for rel in FL.LEDGER:
        assert FL.view(rel, FL.now_text(rel)) == FL.base_text(rel), rel
    text = "x = 1\ny = 2\n"
    FL.UNDO_W2B["tests/_probe.py"] = [("y = 2\n", "y = 3\n")]
    try:
        assert FL.undo_w2b("tests/_probe.py", "x = 1\ny = 3\n") == text and FL.undo_w2b("tests/other.py", "z\r\n") == "z\n"
    finally:
        del FL.UNDO_W2B["tests/_probe.py"]
