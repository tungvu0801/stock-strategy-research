"""
Wave 2 PR B - the Stage 6.1 session classifier v2: its scope pins and freeze bookkeeping (WAVE2_PRB_DESIGN_REV3_4.md Revision 3.4.1 sections 7
and 8; WAVE2_PRB_TEST_PLAN_REV3_4.md section 3: T-W04B, T-W05B, F01B ... F03B), as every stage since 6.8 pins its own delta.

  T-W04B      the production diff since 5d6f7dd is exactly PR A's files plus PR B's five, and PR B's added lines carry no PR C token
  T-W05B      the closed list of functions PR B may change or add (design 7) against 50cb2d7b, and no new module-level statement
  T-W06B      D-5 (operator round 3): cumulative() and REASON_TEXT differ from 50cb2d7b by exactly the unreadable-evidence exclusion
  F01B..F03B  PR B's change set, its declared test changes (SEMANTIC_TESTS_W2B) and every bookkeeping edit of an older test file, exactly
  F04B        the Stage 6.1 Browser Harness flow's k = 3 observations (operator round 3), exactly; T-A42's pinned lines stay in place
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
HARNESS_B = {"browser_tests/flows.py"}                                                         # operator round 3: the Stage 6.1 harness flow (a LEDGER file; F04B)
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
    "forward_validation/service.py": ({FN + n for n in ("_snapshot", "_record_session", "_finalize", "_session_observations", "_backfill_not_run", "cumulative")},   # cumulative: D-5 (T-W06B)
                                      {FN + n for n in ("_confirmed", "_suspects", "_clock_check", "_restart_times", "_settled", "_row_evidence", "_duplicate_evidence",
                                                        "_live_host_evidence", "_assess", "_account_days", "_evidence_read_failed")}),
    "forward_capture/health.py": ({"compute", "=REASON_TEXT"}, {"_auto_disarm_exempt"}),                         # =REASON_TEXT: D-5 (T-W06B)
}
PINNED_B = [("forward_validation/service.py", FN + n) for n in ("observe", "_early_stop", "attention", "_detect_restart", "_previous_sample", "_live_host_attempts",
                                                                "_gate_violations", "order_reliability", "freshness", "broker_health", "daily_summary")] + \
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


def named_assign(text, name):
    """Wave 2 PR C: the value of the module-level assignment `name` (an AST node, for literal_eval)."""
    return next(n.value for n in ast.parse(text).body if isinstance(n, ast.Assign) and any(isinstance(t, ast.Name) and t.id == name for t in n.targets))


def digest(dump: str) -> str:
    return hashlib.sha256(dump.encode()).hexdigest()


def added_lines(rel):
    base = set(at_base(rel).splitlines())
    return "\n".join(l for l in FL.pre_w2c(rel, FL.pre_ex06(rel, now_text(rel))).splitlines() if l not in base)   # Wave 2 PR C: PR B's lines (PR C's own: T-W04C)


def prb_files(base):
    """Wave 2 PR C: the change set between `base` and main after PR #52 (fixed commits). PR C's delta: tests/test_wave2_prc.py."""
    return set(_git("diff", "--name-only", base, FL.BASE_W2C).split())


# ================================================================ T-W04B, T-W05B: the allow-list and the closed function list ================

def test_TW04B_the_production_diff_is_pr_a_plus_pr_b_and_pr_b_adds_no_pr_c_or_order_token():
    prod = {p for p in prb_files(FL.BASE_W2A) if not p.startswith("tests/") and p != ".github/ci/pytest_groups.json" and p not in HARNESS_B}   # the harness: F04B
    assert prod == PR_A_FILES | PR_B_FILES, {"unexpected": sorted(prod - PR_A_FILES - PR_B_FILES), "missing": sorted((PR_A_FILES | PR_B_FILES) - prod)}
    mine = {p for p in prb_files(FL.BASE_W2B) if not p.startswith("tests/") and p != ".github/ci/pytest_groups.json" and p not in HARNESS_B}
    assert mine == PR_B_FILES, sorted(mine ^ PR_B_FILES)
    for rel in sorted(PR_B_FILES):
        added = added_lines(rel)
        assert added and not [t for t in PR_C_TOKENS_B if t in added], rel
        assert not [t for t in N01_TOKENS if t in added] and "disable_auto(" not in added, rel   # V-18: the merged bans, re-run on PR B's lines
        assert not re.search(r"\brequests\b|robinhood|update_intent|set_mode\(|# >>> Stage 6\.1", added), rel   # 6.10A F05's list (health.py is a 6.10A file)
    lit = lambda rel, name: ast.literal_eval(named_assign(FL.pre_w2c(rel, FL.pre_ex06(rel, now_text(rel))), name))  # noqa: E731 - Wave 2 PR C: PR B's text (PR C adds run_id / frozen_hash)
    V2_SAMPLE_COLUMNS, V2_SESSION_COLUMNS = lit("database/forward_validation_migrations.py", "V2_SAMPLE_COLUMNS"), lit("database/forward_validation_migrations.py", "V2_SESSION_COLUMNS")
    SAMPLE_COLS = lit("forward_validation/store.py", "SAMPLE_COLS")
    assert not {"run_id", "frozen_hash"} & ({c for c, _ in V2_SAMPLE_COLUMNS} | set(SAMPLE_COLS) | {c for c, _ in V2_SESSION_COLUMNS})
    assert not {p for p in mine if p.startswith(("safety/", "cash_flow/", "autorun/", "execution/", "setups/", "proposals/", "frontend/", "strategy_registry/"))}
    assert "config.py" not in changed_files(FL.BASE_W2B) and {p for p in mine if p.startswith("forward_capture/")} == {"forward_capture/health.py"}


def test_TW05B_only_the_listed_functions_changed_and_only_the_listed_names_are_new():
    for rel, (may_change, may_add) in CLOSED_LIST_B.items():
        now, base = named_nodes(FL.pre_w2c(rel, FL.pre_ex06(rel, now_text(rel)))), named_nodes(at_base(rel))
        assert set(base) <= set(now), (rel, sorted(set(base) - set(now)))                          # nothing of 50cb2d7b disappears
        assert set(now) - set(base) == may_add, (rel, sorted((set(now) - set(base)) ^ may_add))     # exactly the listed names are new
        changed = {k for k in base if digest(base[k]) != digest(now[k])}
        assert changed <= may_change, (rel, sorted(changed - may_change))                           # every other function hash-equal
        assert sorted(unnamed(FL.pre_w2c(rel, FL.pre_ex06(rel, now_text(rel))))) == sorted(unnamed(at_base(rel))), rel                 # no new module-level statement (imports go inside)
    store_class = ast.parse(FL.pre_w2c("forward_validation/store.py", FL.pre_ex06("forward_validation/store.py", now_text("forward_validation/store.py")))).body
    cls = next(n for n in store_class if isinstance(n, ast.ClassDef) and n.name == "EvidenceSourceAbsent")
    assert [ast.unparse(b) for b in cls.bases] == ["ForwardValidationStoreError"] and not cls.keywords
    for rel, name in PINNED_B:
        assert digest(named_nodes(FL.pre_w2c(rel, FL.pre_ex06(rel, now_text(rel))))[name]) == digest(named_nodes(at_base(rel))[name]), (rel, name)
    probe = named_nodes("class A(B):\n    x = 1\n    def f(self):\n        pass\n")
    assert set(probe) == {"class A", "A.f"} and named_nodes("class A(C):\n    x = 1\n")["class A"] != probe["class A"]   # bases and body are seen
    assert len(unnamed('"""doc"""\nimport os\nx = 1\nA.f = 2\n')) == 2


D5_CUMULATIVE = [                                                                              # D-5: (50cb2d7b, PR B) in cumulative(), each exactly once
    ('        completed = by["HEALTHY"] + by["DEGRADED"] + by["FAILED"]\n',
     '        unreadable = sum(1 for s in sessions if s["session_status"] == "DEGRADED" and "EVIDENCE_UNREADABLE" in (s.get("notes") or []))   # D-5: never acceptable\n'
     '        completed = by["HEALTHY"] + by["DEGRADED"] + by["FAILED"] - unreadable\n'),
    ('and not any(a["critical"] for a in att) and by["FAILED"] == 0\n', 'and not any(a["critical"] for a in att) and by["FAILED"] == 0 and unreadable == 0\n'),
    ('"not_run": by["NOT_RUN"], "in_progress": by["OPEN"],\n', '"not_run": by["NOT_RUN"], "in_progress": by["OPEN"], "unreadable": unreadable,\n'),
]
D5_REASON = ("FV_SESSION_EVIDENCE_UNREADABLE", "the Stage 6.1 session evidence was unreadable (EVIDENCE_UNREADABLE): never acceptable evidence")
D5_HEALTH = ('    if fv_status == "DEGRADED" and "EVIDENCE_UNREADABLE" in ((fv_row or {}).get("notes") or []):\n'
             '        inv.append("FV_SESSION_EVIDENCE_UNREADABLE")                       # Wave 2 PR B (D-5): unreadable evidence never qualifies; not a FAILED record\n')


def fn_text(text, qual):
    """The source lines of a top-level function or a method ('Class.name'), as written."""
    cls, name = qual.split(".") if "." in qual else (None, qual)
    for n in ast.parse(text).body:
        for m in ([n] if cls is None else (n.body if isinstance(n, ast.ClassDef) and n.name == cls else [])):
            if isinstance(m, ast.FunctionDef) and m.name == name:
                return "\n".join(text.splitlines()[m.lineno - 1:m.end_lineno]) + "\n"
    raise AssertionError(qual)


def test_TW06B_d5_unreadable_evidence_is_excluded_from_acceptance_by_exactly_the_declared_lines():
    rel = "forward_validation/service.py"
    now, base = fn_text(FL.pre_w2c(rel, FL.pre_ex06(rel, now_text(rel))), FN + "cumulative"), fn_text(at_base(rel), FN + "cumulative")   # Wave 2 PR C: PR B's text; the current text keeps D-5: T-W06C
    undone = now
    for old, new in D5_CUMULATIVE:
        assert undone.count(new) == 1 and base.count(old) == 1, new[:80]
        undone = undone.replace(new, old)
    assert undone == base                                                                       # cumulative() is 50cb2d7b's plus the D-5 lines, nothing else
    rel = "forward_capture/health.py"
    reasons = lambda t: ast.literal_eval(next(n for n in ast.parse(t).body if isinstance(n, ast.Assign) and getattr(n.targets[0], "id", None) == "REASON_TEXT").value)  # noqa: E731
    assert reasons(now_text(rel)) == {**reasons(at_base(rel)), D5_REASON[0]: D5_REASON[1]} and D5_REASON[0] not in reasons(at_base(rel))
    comp = fn_text(now_text(rel), "compute")
    assert comp.count(D5_HEALTH) == 1 and comp.count("FV_SESSION_EVIDENCE_UNREADABLE") == 1 and "FV_SESSION_EVIDENCE_UNREADABLE" not in at_base(rel)
    assert comp.index('inv.append("FV_SESSION_FAILED" if fv_status == "FAILED" else "FV_SESSION_NOT_RUN")') < comp.index(D5_HEALTH)   # FAILED kept, never relabelled


# ================================================================ F: scope and bookkeeping (as every stage since 6.8) =========================

def test_F01B_the_change_set_is_exactly_the_declared_files_and_no_strategy_risk_broker_or_market_data_file_changed():
    assert FL.BASE_W2B == "50cb2d7bc0384f1a020c01c9ba22c52c1283f339" and subprocess.run(["git", "cat-file", "-e", f"{FL.BASE_W2B}^{{commit}}"], cwd=ROOT).returncode == 0
    changed = changed_files(FL.BASE_W2B)
    both_new, both_edited = FL.NEW_W2B | FL.NEW_W2C | FL.NEW_EX06 | FL.NEW_XSS01, FL.EDITED_W2B | (FL.EDITED_W2C - FL.NEW_W2B) | (FL.EDITED_EX06 - FL.NEW_W2B - FL.NEW_W2C) | (FL.EDITED_XSS01 - FL.NEW_W2B - FL.NEW_W2C - FL.NEW_EX06)   # Wave 2 PR C / EX-06: its own sets (its edits of PR B's new files stay new since BASE_W2B), pinned by tests/test_wave2_prc.py
    assert changed == both_new | both_edited, {"unexpected": sorted(changed - both_new - both_edited), "missing": sorted((both_new | both_edited) - changed)}
    in_base = set(_git("ls-tree", "-r", "--name-only", FL.BASE_W2B).split())
    assert {p for p in changed if p not in in_base} == both_new and {p for p in changed if p in in_base} == both_edited
    assert not _git("diff", "--name-only", "--diff-filter=D", FL.BASE_W2B).split()                 # nothing of 50cb2d7b is deleted
    app = {p for p in FL.EDITED_W2B | FL.NEW_W2B if not p.startswith("tests/")}
    assert app == PR_B_FILES | HARNESS_B | {".github/ci/pytest_groups.json"} and set(FL.SEMANTIC_W2B) == PR_B_FILES and FL.INSTRUMENTATION_W2B == {}
    assert all(len(v) > 40 for v in FL.SEMANTIC_W2B.values())
    marker = re.compile(r"^\s*(#|//|<!--)\s*(>>>|<<<) ")                                       # a marked block an older freeze test would strip unseen
    for rel in sorted(changed):
        base = set(at_base(rel).splitlines()) if rel in in_base else set()
        assert not [l for l in now_text(rel).splitlines() if l not in base and marker.match(l)], rel
    prb_groups = subprocess.run(["git", "show", f"{FL.BASE_W2C}:.github/ci/pytest_groups.json"], cwd=ROOT, capture_output=True).stdout.decode("utf-8")   # Wave 2 PR C: the manifest as PR #52 left it (PR C's entry: F01C)
    groups, base_groups = json.loads(prb_groups)["groups"], json.loads(at_base(".github/ci/pytest_groups.json"))["groups"]
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
        b, n = defs(at_base(rel)), defs(FL.undo_w2c(rel, FL.undo_ex06(rel, FL.undo_xss01(rel, now_text(rel)))))                          # Wave 2 PR C: its own in-place edits undone first (F02C)
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
        assert FL.undo_w2b(rel, FL.undo_w2c(rel, FL.undo_ex06(rel, FL.undo_xss01(rel, now_text(rel))))) == at_base(rel), rel           # now -> (F03C) 875bc06d -> 50cb2d7b, byte for byte
    main_fl = at_base("tests/freeze_ledger.py")
    fl = subprocess.run(["git", "show", f"{FL.BASE_W2C}:tests/freeze_ledger.py"], cwd=ROOT, capture_output=True).stdout.decode("utf-8").replace("\r\n", "\n")   # Wave 2 PR C: the ledger as PR #52 left it
    assert fl.count("LEDGER = {") == 1 and fl.count("BASE_W2B = ") == 1                       # the anchors every older ledger test uses stay unique
    led = lambda t: t[t.index("\nLEDGER = {\n") + 1:t.index("}\n", t.index("\nLEDGER = {\n")) + 2]  # noqa: E731
    main_ledger = ast.literal_eval(led(main_fl)[len("LEDGER = "):])
    start = fl.index("\nWave 2 PR B (the Stage 6.1 session classifier v2")
    rebuilt = fl.replace(fl[start:fl.index('"""', start)], "", 1).replace(" + NEW_W2A + NEW_W2B plus this ledger.", " + NEW_W2A plus this ledger.", 1)
    rebuilt = rebuilt.replace(rebuilt[rebuilt.index("BASE_W2B = "):rebuilt.index("\nLEDGER = {\n") + 1], "", 1)
    rebuilt = rebuilt.replace(rebuilt[rebuilt.index("\n\ndef pre_w2b("):rebuilt.index("\n\ndef unchanged(")], "", 1)
    rebuilt = rebuilt.replace(led(rebuilt), led(main_fl), 1)
    assert rebuilt == main_fl                                                                 # the paragraph, one line, the declarations, two helpers and the ledger
    prb_ledger = ast.literal_eval(led(fl)[len("LEDGER = "):])                                 # Wave 2 PR C: PR B's ledger; PR C's re-hashes are its F03C
    assert set(prb_ledger) - set(main_ledger) == FL.NEW_LEDGER_W2B and set(main_ledger) <= set(prb_ledger)
    assert {k for k in main_ledger if main_ledger[k] != prb_ledger[k]} == FL.EDITED_W2B & set(main_ledger)   # every re-hash is a declared PR B edit
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


FLOWS_EDIT_B = [                                                                               # F04B: (50cb2d7b, PR B) in browser_tests/flows.py, each exactly once
    ("against the REAL observer (one observation through the service, no thread),\n", "against the REAL observer (k = 3 observations through the service, no thread),\n"),
    ("    from datetime import datetime, timezone\n    from api.routes.trader_view import stream\n    st = stream()\n    fv, xsvc = st.forward_validation, st.execution\n",
     "    from datetime import datetime, timedelta, timezone\n    from api.routes.trader_view import stream\n    st = stream()\n    fv, xsvc = st.forward_validation, st.execution\n"),
    ('    out = fv.observe(now, "TEST")\n'
     '    c.r.eq("one observation through the real service records a sample, opens the session record and finds no attention condition", [out["result"], out["session_status"], out["attention"]], ["OK", "OPEN", []])\n',
     '    outs = [fv.observe(now + timedelta(seconds=s), "TEST") for s in (-120, -60, 0)]      # Wave 2 PR B: k = 3 plausible observations confirm the session (B-12)\n'
     '    c.r.eq("three observations through the real service record samples; only the third (confirmed, k = 3) opens the session record; none finds an attention condition", '
     '[[o["result"] for o in outs], [o["session_status"] for o in outs], [o["attention"] for o in outs]], [["OK", "OK", "OK"], [None, None, "OPEN"], [[], [], []]])\n'),
]


def test_F04B_the_harness_flow_gives_k3_observations_exactly_and_keeps_t_a42s_lines_in_place():
    rel = "browser_tests/flows.py"
    now, base = FL.pre_ex06(rel, now_text(rel)), at_base(rel)                               # EX-06: PR B's text (EX-06's own edit: T-X20)
    undone = now
    for old, new in reversed(FLOWS_EDIT_B):
        assert undone.count(new) == 1 and base.count(old) == 1, new[:80]
        undone = undone.replace(new, old)
    assert undone == base and rel in FL.EDITED_W2B and rel in FL.LEDGER and FL.sha(now_text(rel)) == FL.LEDGER[rel]   # the edit alone; the ledger holds the current text (EX-06: F03X)
    assert now.count("\n") == base.count("\n")                                                  # no line moves: T-A42 (ii) reads flows[2569:2590] by position
    t_a42 = subprocess.run(["git", "show", f"{FL.BASE_W2A}:{rel}"], cwd=ROOT, capture_output=True).stdout.decode("utf-8").replace("\r\n", "\n").splitlines()
    assert now.splitlines()[2569:2590] == t_a42[2569:2590] and len(t_a42[2569:2590]) == 21
    edit = "".join(n for _, n in FLOWS_EDIT_B)
    assert not [x for x in N01_TOKENS if x in edit] and "disable_auto(" not in edit and edit.count("fv.observe(") == 1 and '"TEST")' in edit   # observe only, TEST trigger
    assert "c.r.check(" not in edit and edit.count("c.r.eq(") == 1 and "[None, None, \"OPEN\"]" in edit   # one equality, stricter than v1's: unconfirmed, unconfirmed, OPEN
