"""
Wave 2 PR A - scaffolding and preservation (WAVE2_REMEDIATION_DESIGN.md sections 3.11 G-07, 3.12.2, 6.1; WAVE2_TEST_AND_ACCEPTANCE_PLAN.md section
3.3: T-W01 ... T-W05, T-B19, T-R05-pre) and the PR's freeze bookkeeping (F01 ... F03, as every stage since 6.8 pins its own delta).

  T-W01..W03  a pinned (block hash -> execution.VERSION) table: a ledger-listed decision or accounting block that changes without a new
              execution.VERSION fails the suite; PR A bumps execution_v1 -> execution_v2
  T-W04       the production diff since 5d6f7dd is exactly PR A's file list and carries no PR C token
  T-W05       the closed list of functions PR A may change or add (design 3.12.2), with the normalized checks of J-04
  T-B19       the frozen strategy-controls hash and its pins are unchanged
  T-R05-pre   reconciliation output identical to 5d6f7dd over 12 scenarios x 5 cycles (tests/wave2_pra_golden.json)
  F01..F03    the change set, the declared semantics and every bookkeeping edit of an older test file, exactly
"""
import ast
import hashlib
import re
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
import freeze_ledger as FL  # noqa: E402
from test_execution_60 import execution_config, no_execution_wire  # noqa: E402,F401 - the rig's two autouse fixtures
import wave2_pra_support as S  # noqa: E402

sys.path.insert(0, str(ROOT))
import execution as X  # noqa: E402
import forward_validation as FV  # noqa: E402

GOLDEN = __import__("json").loads((ROOT / "tests" / "wave2_pra_golden.json").read_text(encoding="utf-8"))
PR_A_FILES = {"execution/service.py", "execution/store.py", "execution/__init__.py", "forward_validation/service.py", "forward_validation/attention_signature.py",
              "forward_validation/reports.py", "api/routes/forward_validation.py", "tools/verify_exit_fills.py"}   # design section 2: W2-1 and X2 (core)
PR_C_TOKENS = ("forward_runs", "forward_run_events", "classifier_version", "assessment_json", "bot_exposed", "bot_orders")   # T-W04 (G-07)
FN = "ForwardValidationService."
CLOSED_LIST = {                                                                                  # design 3.12.2: file -> (may change, may add)
    "execution/service.py": ({"ExecutionService._apply_order", "ExecutionService._apply_fill"}, {"recompute_pnl"}),
    "execution/store.py": (set(), {"ExecutionStore.exit_intents_for_position", "ExecutionStore.accounting_view", "ExecutionStore.pnl_watermark_rows"}),
    "execution/__init__.py": ({"=VERSION"}, set()),
    "forward_validation/service.py": ({FN + n for n in ("__init__", "observe", "_early_stop", "_trade", "closed_trades", "trace", "daily_summary", "cumulative")},
                                      {FN + n for n in ("verification", "_fail_closed_trade", "_pnl_status", "_null_pnl", "pnl_watermark", "bracketed_summary", "fresh_daily",
                                                        "fresh_dailies", "_pnl_content", "_persist_stale_summaries")} | {"=BRACKET_ATTEMPTS", "=SUMMARY_REBUILDS_PER_TICK"}),
    "api/routes/forward_validation.py": ({"get_session", "get_dailies", "get_daily", "get_daily_report_latest", "get_daily_report", "get_weekly_report"}, set()),
    "forward_validation/reports.py": ({"=TRADE_COLS", "=DAILY_COLS", "_flat_daily", "export_rows", "daily_report", "weekly_report"}, {"_pnl_text"}),
}


def _git(*a):
    return subprocess.run(["git", *a], cwd=ROOT, capture_output=True, text=True).stdout


def at_base(rel):
    run = subprocess.run(["git", "show", f"{FL.BASE_W2A}:{rel}"], cwd=ROOT, capture_output=True)
    assert run.returncode == 0, f"{rel} is not in {FL.BASE_W2A} (fetch-depth 0 is required)"
    return run.stdout.decode("utf-8").replace("\r\n", "\n")


def now_text(rel):
    return (ROOT / rel).read_text(encoding="utf-8").replace("\r\n", "\n")


def named_nodes(text):
    """qualname -> AST node: top-level functions and classes' methods, and '=NAME' for every module-level assignment."""
    out = {}
    for n in ast.parse(text).body:
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
            out[n.name] = n
        elif isinstance(n, ast.ClassDef):
            for m in n.body:
                if isinstance(m, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    out[f"{n.name}.{m.name}"] = m
        elif isinstance(n, (ast.Assign, ast.AnnAssign)):
            for t in (n.targets if isinstance(n, ast.Assign) else [n.target]):
                if isinstance(t, ast.Name):
                    out["=" + t.id] = n
    return out


def unnamed(text):
    """L-07: every top-level and class-body statement named_nodes() does not see (imports, expressions, attribute assignments); not the module docstring."""
    tree = ast.parse(text)
    body = tree.body[1:] if tree.body and isinstance(tree.body[0], ast.Expr) and isinstance(getattr(tree.body[0], "value", None), ast.Constant) else tree.body
    def named(n):                                                                           # M-01: an assignment is named only when every target is a plain name
        return isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) or (
            isinstance(n, (ast.Assign, ast.AnnAssign)) and all(isinstance(t, ast.Name) for t in (n.targets if isinstance(n, ast.Assign) else [n.target])))
    out = [ast.dump(n) for n in body if not named(n)]
    for c in (n for n in tree.body if isinstance(n, ast.ClassDef)):
        out += [c.name + ":" + ast.dump(m) for m in c.body if not isinstance(m, (ast.FunctionDef, ast.AsyncFunctionDef))]
    return out


NEW_IMPORTS = {"forward_validation/service.py": ["import json", "from execution.engine import D as XD, money as xmoney",
                                                  "from forward_validation.attention_signature import HUMAN_ACTORS, signature"]}   # K-11: the only unnamed additions


def digest(node) -> str:
    return hashlib.sha256(ast.dump(node).encode()).hexdigest()


# ================================================================ T-W01 ... T-W03: the execution.VERSION pin =================================

def block_hashes(text_of) -> dict:
    out = {}
    for key in FL.VERSION_BLOCKS_W2A:
        rel, name = key.split("::")
        text = text_of(rel)
        node = named_nodes(text).get(name) if text is not None else None
        out[key] = digest(node) if node is not None else "ABSENT"
    return out


def version_pin_problems(pins: dict, version: str, current: dict) -> list:
    """The pinned table's rule: the CURRENT version's entry must hash every listed block exactly as it is now."""
    if version not in pins:
        return [f"execution.VERSION {version!r} has no pinned block table: a listed block changed? bump the version and pin it"]
    return sorted(k for k in current if pins[version].get(k) != current[k])


def test_TW01_a_listed_block_changed_without_a_new_execution_version_fails():
    assert version_pin_problems(FL.VERSION_PINS_W2A, X.VERSION, block_hashes(now_text)) == []


def test_TW02_the_rule_fails_an_unbumped_change_and_passes_a_bumped_one():
    current = block_hashes(now_text)
    changed = {**current, "execution/service.py::ExecutionService._apply_fill": "f" * 64}            # a block edited after its pin
    assert version_pin_problems(FL.VERSION_PINS_W2A, X.VERSION, changed) == ["execution/service.py::ExecutionService._apply_fill"]
    assert version_pin_problems({**FL.VERSION_PINS_W2A, "execution_v3": changed}, "execution_v3", changed) == []
    assert version_pin_problems(FL.VERSION_PINS_W2A, "execution_v9", current) != []


def test_TW03_pr_a_bumps_the_execution_version_and_the_v1_pins_are_the_baseline():
    pra_version = ast.literal_eval(named_nodes(FL.pre_w2c("execution/__init__.py", FL.pre_ex06("execution/__init__.py", now_text("execution/__init__.py"))))["=VERSION"].value)   # Wave 2 PR C: PR A's bump as main left it (PR C's execution_v3: its T-W01C)
    assert (pra_version, at_base("execution/__init__.py").count('VERSION = "execution_v1"')) == ("execution_v2", 1)
    base = block_hashes(lambda rel: at_base(rel) if rel in set(_git("ls-tree", "-r", "--name-only", FL.BASE_W2A).split()) else None)
    assert FL.VERSION_PINS_W2A["execution_v1"] == base
    moved = {k for k in base if base[k] != FL.VERSION_PINS_W2A["execution_v2"][k]}
    assert moved == {"execution/service.py::ExecutionService._apply_order", "execution/service.py::ExecutionService._apply_fill", "execution/service.py::recompute_pnl",
                     "forward_validation/service.py::ForwardValidationService._early_stop"} | {k for k in base if k.startswith("forward_validation/attention_signature.py::")}


# ================================================================ T-W04, T-W05: the allow-list and the closed function list ==================

def changed_files():
    c = set(_git("diff", "--name-only", FL.BASE_W2A).split()) | set(_git("ls-files", "--others", "--exclude-standard").split())
    return {p for p in c if not p.startswith(("scratchpad/", "runtime/", "data/market_intel", "logs/", "browser_tests/artifacts/")) and (ROOT / p).exists()}


def pra_files():
    """Wave 2 PR B: PR A's own change set, between the two fixed commits (main before / after PR #51). PR B's delta: tests/test_wave2_prb.py."""
    return set(_git("diff", "--name-only", FL.BASE_W2A, FL.BASE_W2B).split())


def test_TW04_the_production_diff_is_exactly_pr_a_and_names_no_pr_c_object():
    prod = {p for p in pra_files() if not p.startswith("tests/") and p != ".github/ci/pytest_groups.json"}
    assert prod == PR_A_FILES, {"unexpected": sorted(prod - PR_A_FILES), "missing": sorted(PR_A_FILES - prod)}
    in_base = set(_git("ls-tree", "-r", "--name-only", FL.BASE_W2A).split())
    for rel in sorted(prod):
        base = set(at_base(rel).splitlines()) if rel in in_base else set()
        added = "\n".join(l for l in FL.pre_w2b(rel, FL.pre_w2c(rel, FL.pre_ex06(rel, FL.pre_gate(rel, FL.pre_rec(rel, now_text(rel)))))).splitlines() if l not in base)
        assert not [t for t in PR_C_TOKENS if t in added], rel
    assert {"execution/engine.py", "execution/broker.py", "execution/exposure_gate.py", "config.py"}.isdisjoint(pra_files())
    assert not {p for p in pra_files() if p.startswith(("safety/", "cash_flow/", "autorun/", "forward_capture/", "setups/", "proposals/", "frontend/"))}


def _normalized(rel, name, text):
    """J-04: the three functions whose allowed change is narrower than the function itself, mapped back to the baseline form."""
    node = named_nodes(text)[name]
    if hasattr(node, "decorator_list"):
        node.decorator_list = []                                                                # the route decorators are pinned with the rest of the file
    if name == FN + "observe":
        body = list(node.body)
        persist = [s for s in body if isinstance(s, ast.Try) and any(getattr(c.func, "attr", None) == "_persist_stale_summaries" for c in ast.walk(s) if isinstance(c, ast.Call))]
        assert len(persist) == 1 and body[body.index(persist[0]) + 1] is body[-2], "observe: exactly one Try, placed right before self.last_observation = out"
        node.body = [s for s in body if s is not persist[0]]
        return ast.dump(node)
    seg = ast.get_source_segment(text, node)
    seg = seg.replace("service.fresh_dailies(limit=100000, newest_first=False)", "st.dailies(limit=100000, newest_first=False)")
    seg = re.sub(r"svc\.fresh_daily\((\w+)\)", r"svc.store.daily(\1) or svc.daily_summary(\1)", seg)
    seg = re.sub(r"svc\.fresh_dailies\(([^)]*)\)", r"svc.store.dailies(\1)", seg)
    return ast.dump(ast.parse(seg).body[0])


def test_TW05_only_the_listed_functions_changed_and_only_the_listed_names_are_new():
    for rel, (may_change, may_add) in CLOSED_LIST.items():
        now, base = named_nodes(FL.pre_w2b(rel, FL.pre_w2c(rel, FL.pre_ex06(rel, FL.pre_gate(rel, FL.pre_rec(rel, now_text(rel))))))), named_nodes(at_base(rel))
        assert set(base) <= set(now), (rel, sorted(set(base) - set(now)))                          # nothing of 5d6f7dd disappears
        assert set(now) - set(base) <= may_add, (rel, sorted(set(now) - set(base) - may_add))        # nothing new outside the list
        changed = {k for k in base if digest(base[k]) != digest(now[k])}
        assert changed <= may_change, (rel, sorted(changed - may_change))                           # every pinned function hash-equal
        u_now, u_base = unnamed(FL.pre_w2b(rel, FL.pre_w2c(rel, FL.pre_ex06(rel, FL.pre_gate(rel, FL.pre_rec(rel, now_text(rel))))))), unnamed(at_base(rel))
        added = [ast.dump(ast.parse(s).body[0]) for s in NEW_IMPORTS.get(rel, [])]
        assert sorted(u_now) == sorted(u_base + added), (rel, sorted(set(u_now) ^ set(u_base + added)))   # L-07: imports and other statements too
    probe = unnamed("x = 1\nclass A:\n    def f(self):\n        pass\nA.f = lambda self: None\nA.g, h = 1, 2\n")       # M-01: a rebinding of a pinned method is seen
    assert len(probe) == 2 and all("Attribute" in u for u in probe)
    narrow = [("forward_validation/service.py", FN + "observe"), ("forward_validation/reports.py", "export_rows")] + [("api/routes/forward_validation.py", n) for n in CLOSED_LIST["api/routes/forward_validation.py"][0]]
    for rel, name in narrow:
        base_node = named_nodes(at_base(rel))[name]
        base_node.decorator_list = []
        assert _normalized(rel, name, FL.pre_w2b(rel, FL.pre_w2c(rel, FL.pre_ex06(rel, FL.pre_gate(rel, FL.pre_rec(rel, now_text(rel))))))) == ast.dump(base_node), (rel, name)
    for col, extra in (("TRADE_COLS", ("verified",)), ("DAILY_COLS", ("pnl_status", "unverified"))):
        val = lambda t: tuple(ast.literal_eval(named_nodes(t)["=" + col].value))  # noqa: E731
        assert val(now_text("forward_validation/reports.py")) == val(at_base("forward_validation/reports.py")) + extra, col


# ================================================================ T-B19, T-R05-pre ============================================================

def test_TB19_the_frozen_strategy_controls_and_their_pins_are_unchanged():
    out = subprocess.run([sys.executable, "-c", "import forward_validation as FV; print(FV.frozen_hash())"], cwd=ROOT, capture_output=True, text=True)   # a clean process
    assert out.stdout.split() == ["b5d4a56ef5bba61f8250b3cd25182cc1655738adf0605a77671fd029883f8530"], out.stderr[-600:]
    t61, b61 = named_nodes(now_text("tests/test_forward_validation_61.py")), named_nodes(at_base("tests/test_forward_validation_61.py"))
    for name in [k for k in b61 if k.startswith(("test_Z01_", "test_F02_"))]:
        assert digest(t61[name]) == digest(b61[name]), name
    assert now_text("forward_validation/__init__.py") == at_base("forward_validation/__init__.py") and now_text("config.py") == at_base("config.py")


def test_TR05pre_reconciliation_output_is_identical_to_the_baseline(tmp_path):
    from test_execution_60 import cyc, make_stack
    for fn in S.reconciliation_scenarios():
        st = make_stack(tmp_path / fn.__name__)
        fn(st)
        snaps = []
        for _ in range(5):
            cyc(st)
            snaps.append(S.stack_snapshot(st))
        assert {"steps": len(snaps), "sha256": S.trace_digest(snaps)} == GOLDEN["stack"][fn.__name__], fn.__name__
    pre_c = FL.pre_w2c("execution/service.py", FL.pre_ex06("execution/service.py", now_text("execution/service.py")))              # Wave 2 PR C: its W2-6 _reconcile detail is its own pin (T-W05C, T-W01C)
    assert named_nodes(pre_c)["ExecutionService._reconcile"] and digest(named_nodes(pre_c)["ExecutionService._reconcile"]) == \
        digest(named_nodes(at_base("execution/service.py"))["ExecutionService._reconcile"])


# ================================================================ F: scope and bookkeeping (as every stage since 6.8) =========================
NEW_LEDGER_W2A = {"execution/__init__.py", "execution/store.py"}                               # main's files Wave 2 PR A registers (unchanged since BASE_68)


def test_F01_the_change_set_is_exactly_the_declared_files_and_no_strategy_risk_broker_or_market_data_file_changed():
    assert FL.BASE_W2A == "5d6f7dd1e1beb90fc5b2909a7dafabfd8e932a6a" and subprocess.run(["git", "cat-file", "-e", f"{FL.BASE_W2A}^{{commit}}"], cwd=ROOT).returncode == 0
    changed = changed_files()
    both_new, both_edited = FL.NEW_W2A | FL.NEW_W2B | FL.NEW_W2C | FL.NEW_EX06 | FL.NEW_XSS01 | FL.NEW_PANEL | FL.NEW_J04 | FL.NEW_GATE | FL.NEW_REC | FL.NEW_EXP, FL.EDITED_W2A | (FL.EDITED_W2B - FL.NEW_W2A) | (FL.EDITED_W2C - FL.NEW_W2A - FL.NEW_W2B) | (FL.EDITED_EX06 - FL.NEW_W2A - FL.NEW_W2B - FL.NEW_W2C) | (FL.EDITED_XSS01 - FL.NEW_W2A - FL.NEW_W2B - FL.NEW_W2C - FL.NEW_EX06) | (FL.EDITED_PANEL - FL.NEW_W2A - FL.NEW_W2B - FL.NEW_W2C - FL.NEW_EX06 - FL.NEW_XSS01) | (FL.EDITED_J04 - FL.NEW_W2A - FL.NEW_W2B - FL.NEW_W2C - FL.NEW_EX06 - FL.NEW_XSS01 - FL.NEW_PANEL - FL.NEW_J04) | (FL.EDITED_GATE - FL.NEW_W2A - FL.NEW_W2B - FL.NEW_W2C - FL.NEW_EX06 - FL.NEW_XSS01 - FL.NEW_PANEL - FL.NEW_J04 - FL.NEW_GATE) | (FL.EDITED_REC - FL.NEW_W2A - FL.NEW_W2B - FL.NEW_W2C - FL.NEW_EX06 - FL.NEW_XSS01 - FL.NEW_PANEL - FL.NEW_J04 - FL.NEW_GATE - FL.NEW_REC) | (FL.EDITED_EXP - FL.NEW_W2A - FL.NEW_W2B - FL.NEW_W2C - FL.NEW_EX06 - FL.NEW_XSS01 - FL.NEW_PANEL - FL.NEW_J04 - FL.NEW_GATE - FL.NEW_REC - FL.NEW_EXP)   # Wave 2 PR B / PR C / EX-06: their own sets (their edits of earlier new files stay new since BASE_W2A), pinned by tests/test_wave2_prb.py / test_wave2_prc.py
    assert changed == both_new | both_edited, {"unexpected": sorted(changed - both_new - both_edited), "missing": sorted((both_new | both_edited) - changed)}
    in_base = set(_git("ls-tree", "-r", "--name-only", FL.BASE_W2A).split())
    assert {p for p in changed if p not in in_base} == both_new and {p for p in changed if p in in_base} == both_edited
    app = {p for p in FL.EDITED_W2A | FL.NEW_W2A if not p.startswith("tests/")}
    assert app == PR_A_FILES | {".github/ci/pytest_groups.json"} and set(FL.SEMANTIC_W2A) == PR_A_FILES - FL.NEW_W2A and FL.INSTRUMENTATION_W2A == {}
    assert all(len(v) > 40 for v in FL.SEMANTIC_W2A.values())
    marker = re.compile(r"^\s*(#|//|<!--)\s*(>>>|<<<) ")                                       # a marked block an older freeze test would strip unseen
    for rel in sorted(app):
        base = set(at_base(rel).splitlines()) if rel in in_base else set()
        assert not [l for l in FL.pre_w2b(rel, FL.pre_w2c(rel, FL.pre_ex06(rel, FL.pre_gate(rel, FL.pre_rec(rel, now_text(rel)))))).splitlines() if l not in base and marker.match(l)], rel


def test_F02_the_declared_semantics_only_no_order_or_mode_call_added_and_no_threshold_or_frozen_control_moved():
    out = subprocess.run([sys.executable, "-c", "import config, forward_validation as FV; from execution import exposure_gate as G; "
                          "print(FV.frozen_hash()); print(G.contract_hash(G.from_config(config))); print(G.contract_hash(G.GateSettings(enabled=True)))"],
                         cwd=ROOT, capture_output=True, text=True)                              # a clean process: the real configuration
    assert out.stdout.split() == ["b5d4a56ef5bba61f8250b3cd25182cc1655738adf0605a77671fd029883f8530",
                                  "2552bd6639f691b828624250665519cc90c4a00da7f70893dbb635f7551006fa",
                                  "d2819342f6c8ccb1cd4a0ce2bfb5d1b0664f2453770c3e6736f7be420a17aed4"], out.stderr[-600:]
    for rel in ("execution/service.py", "execution/store.py"):
        added = "\n".join(l for l in FL.pre_gate(rel, FL.pre_rec(rel, now_text(rel))).splitlines() if l not in set(at_base(rel).splitlines()))   # the order gate's C-7 edit: tests/test_gate_freeze.py
        assert not re.search(r"post_order|insert_intent|update_intent|update_position|enable_auto|disable_auto|set_mode|submit|_scan_entries\(|_recheck_entry\(|cancel|liquidat|flatten|broker\.", added), rel
    fv_added = "\n".join(l for l in now_text("forward_validation/service.py").splitlines() if l not in set(at_base("forward_validation/service.py").splitlines()))
    assert fv_added.count("disable_auto(") == 0 and now_text("forward_validation/service.py").count("disable_auto(") == at_base("forward_validation/service.py").count("disable_auto(") == 1   # the one action, its line kept, none added
    for rel in FL.SEMANTIC_W2A:
        assert FL.pre_w2a(rel, now_text(rel)) == at_base(rel) and now_text(rel) != at_base(rel), rel   # what older freeze tests read: main's text, exactly
    assert FL.pre_w2a("config.py", "x") == "x"


def test_F03_every_bookkeeping_edit_is_pinned_exactly_and_the_ledger_changes_only_the_declared_entries():
    for rel, edits in REPLAY_W2A.items():
        text = FL.undo_w2b(rel, FL.undo_w2c(rel, FL.undo_ex06(rel, FL.undo_xss01(rel, FL.undo_panel(rel, FL.undo_j04(rel, FL.undo_gate(rel, FL.undo_rec(rel, FL.undo_exp(rel, now_text(rel))))))))))                               # Wave 2 PR B / PR C: their own in-place edits undone first (F03B / F03C)
        for old, new in reversed(edits):
            assert text.count(new) == 1, (rel, new[:100])
            text = text.replace(new, old)
        assert text == at_base(rel), rel
    main_fl = at_base("tests/freeze_ledger.py")
    led = lambda t: t[t.index("\nLEDGER = {") + 1:t.index("}\n", t.index("\nLEDGER = {")) + 2]  # noqa: E731
    main_ledger = ast.literal_eval(led(main_fl)[len("LEDGER = "):])
    assert set(REPLAY_W2A) == {p for p in FL.EDITED_W2A if p.startswith("tests/")} - set(main_ledger) - set(NEW_LEDGER_W2A) - {"tests/freeze_ledger.py"}
    fl = subprocess.run(["git", "show", f"{FL.BASE_W2B}:tests/freeze_ledger.py"], cwd=ROOT, capture_output=True).stdout.decode("utf-8").replace("\r\n", "\n")   # Wave 2 PR B: the ledger as PR #51 left it
    start = fl.index("\nWave 2 PR A (forward-validation accounting")
    rebuilt = fl.replace(fl[start:fl.index('"""', start)], "", 1).replace(" + NEW_W1 + NEW_W2A plus this ledger.", " + NEW_W1 plus this ledger.", 1)
    rebuilt = rebuilt.replace(rebuilt[rebuilt.index("BASE_W2A = "):rebuilt.index("LEDGER = {")], "", 1)
    rebuilt = rebuilt.replace(rebuilt[rebuilt.index("\n\ndef pre_w2a("):rebuilt.index("\n\ndef unchanged(")], "", 1)
    rebuilt = rebuilt.replace(led(rebuilt), led(main_fl), 1)
    assert rebuilt == main_fl                                                                 # the paragraph, one line, the declarations, the helper and the ledger
    pra_ledger = ast.literal_eval(led(fl)[len("LEDGER = "):])                                 # Wave 2 PR B: PR A's ledger; PR B's re-hashes are its F03B
    assert set(pra_ledger) - set(main_ledger) == NEW_LEDGER_W2A and set(main_ledger) <= set(pra_ledger)
    assert {k for k in main_ledger if main_ledger[k] != pra_ledger[k]} == FL.EDITED_W2A & set(main_ledger)   # every re-hash is a declared PR A edit
    for rel in sorted(NEW_LEDGER_W2A):
        assert FL.base_text(rel) == at_base(rel), rel                                        # unchanged from BASE_68 to main: the ledger view IS main's text
    for rel in FL.LEDGER:
        assert FL.view(rel, FL.now_text(rel)) == FL.base_text(rel), rel


REPLAY_W2A = {                                                                               # every edit of a file no ledger entry can hold: (main, Wave 2 PR A)
    'tests/test_bot_activity_68.py': [
        ('    assert new == FL.NEW_68 | FL.NEW_69 | FL.NEW_691 | FL.NEW_610A | FL.NEW_610C | FL.NEW_541 | FL.NEW_611FIX | FL.NEW_OPS | FL.NEW_610D | FL.NEW_542 | FL.NEW_70 | FL.NEW_71 | FL.NEW_72 | FL.NEW_73 | FL.NEW_731 | FL.NEW_74 | FL.NEW_75 | FL.NEW_78 | FL.NEW_78B1 | FL.NEW_W1, {"unexpected_new": sorted(new - FL.NEW_68 - FL.NEW_69 - FL.NEW_691 - FL.NEW_610A - FL.NEW_610C - FL.NEW_541 - FL.NEW_611FIX - FL.NEW_OPS - FL.NEW_610D - FL.NEW_542 - FL.NEW_70 - FL.NEW_71 - FL.NEW_72 - FL.NEW_73 - FL.NEW_731 - FL.NEW_74 - FL.NEW_75 - FL.NEW_78 - FL.NEW_78B1 - FL.NEW_W1),\n                                                                     "missing_new": sorted((FL.NEW_68 | FL.NEW_69 | FL.NEW_691 | FL.NEW_610A | FL.NEW_610C | FL.NEW_541 | FL.NEW_611FIX | FL.NEW_OPS | FL.NEW_610D | FL.NEW_542 | FL.NEW_70 | FL.NEW_71 | FL.NEW_72 | FL.NEW_73 | FL.NEW_731 | FL.NEW_74 | FL.NEW_75 | FL.NEW_78 | FL.NEW_78B1 | FL.NEW_W1) - new)}   # + Stage 6.9 / 6.9.1 / 6.10A / 6.10C / 5.4.1\n',
         '    assert new == FL.NEW_68 | FL.NEW_69 | FL.NEW_691 | FL.NEW_610A | FL.NEW_610C | FL.NEW_541 | FL.NEW_611FIX | FL.NEW_OPS | FL.NEW_610D | FL.NEW_542 | FL.NEW_70 | FL.NEW_71 | FL.NEW_72 | FL.NEW_73 | FL.NEW_731 | FL.NEW_74 | FL.NEW_75 | FL.NEW_78 | FL.NEW_78B1 | FL.NEW_W1 | FL.NEW_W2A, {"unexpected_new": sorted(new - FL.NEW_68 - FL.NEW_69 - FL.NEW_691 - FL.NEW_610A - FL.NEW_610C - FL.NEW_541 - FL.NEW_611FIX - FL.NEW_OPS - FL.NEW_610D - FL.NEW_542 - FL.NEW_70 - FL.NEW_71 - FL.NEW_72 - FL.NEW_73 - FL.NEW_731 - FL.NEW_74 - FL.NEW_75 - FL.NEW_78 - FL.NEW_78B1 - FL.NEW_W1 - FL.NEW_W2A),\n                                                                     "missing_new": sorted((FL.NEW_68 | FL.NEW_69 | FL.NEW_691 | FL.NEW_610A | FL.NEW_610C | FL.NEW_541 | FL.NEW_611FIX | FL.NEW_OPS | FL.NEW_610D | FL.NEW_542 | FL.NEW_70 | FL.NEW_71 | FL.NEW_72 | FL.NEW_73 | FL.NEW_731 | FL.NEW_74 | FL.NEW_75 | FL.NEW_78 | FL.NEW_78B1 | FL.NEW_W1 | FL.NEW_W2A) - new)}   # + Stage 6.9 / 6.9.1 / 6.10A / 6.10C / 5.4.1\n'),
        ('        base, now = FL.base_text(rel).splitlines(), set(FL.now_text(rel).splitlines())\n',
         '        base, now = FL.base_text(rel).splitlines(), set(FL.pre_w2a(rel, FL.now_text(rel)).splitlines())   # Wave 2 PR A: its declared accounting change, pinned by its own tests\n'),
        ('    svc, sch = FL.now_text("execution/service.py"), FL.now_text("execution/scheduler.py")\n',
         '    svc, sch = FL.pre_w2a("execution/service.py", FL.now_text("execution/service.py")), FL.now_text("execution/scheduler.py")\n'),
    ],
    'tests/test_chart_empty_state_611fix.py': [
        ('    assert changed == FL.NEW_611FIX | FL.EDITED_611FIX | FL.NEW_OPS | FL.EDITED_OPS | FL.NEW_610D | FL.EDITED_610D | FL.NEW_542 | FL.EDITED_542 | FL.NEW_70 | FL.EDITED_70 | FL.NEW_71 | FL.EDITED_71 | FL.NEW_72 | FL.EDITED_72 | FL.NEW_73 | FL.EDITED_73 | FL.NEW_731 | FL.EDITED_731 | FL.NEW_74 | FL.EDITED_74 | FL.NEW_75 | FL.EDITED_75 | FL.NEW_78 | FL.EDITED_78 | FL.NEW_78B1 | FL.EDITED_78B1 | FL.NEW_W1 | FL.EDITED_W1\n',
         '    assert changed == FL.NEW_611FIX | FL.EDITED_611FIX | FL.NEW_OPS | FL.EDITED_OPS | FL.NEW_610D | FL.EDITED_610D | FL.NEW_542 | FL.EDITED_542 | FL.NEW_70 | FL.EDITED_70 | FL.NEW_71 | FL.EDITED_71 | FL.NEW_72 | FL.EDITED_72 | FL.NEW_73 | FL.EDITED_73 | FL.NEW_731 | FL.EDITED_731 | FL.NEW_74 | FL.EDITED_74 | FL.NEW_75 | FL.EDITED_75 | FL.NEW_78 | FL.EDITED_78 | FL.NEW_78B1 | FL.EDITED_78B1 | FL.NEW_W1 | FL.EDITED_W1 | FL.NEW_W2A | FL.EDITED_W2A\n'),
    ],
    'tests/test_decision_flow_610c.py': [
        ('    later = FL.NEW_541 | FL.EDITED_541 | FL.NEW_611FIX | FL.EDITED_611FIX | FL.NEW_OPS | FL.EDITED_OPS | FL.NEW_610D | FL.EDITED_610D | FL.NEW_542 | FL.EDITED_542 | FL.NEW_70 | FL.EDITED_70 | FL.NEW_71 | FL.EDITED_71 | FL.NEW_72 | FL.EDITED_72 | FL.NEW_73 | FL.EDITED_73 | FL.NEW_731 | FL.EDITED_731 | FL.NEW_74 | FL.EDITED_74 | FL.NEW_75 | FL.EDITED_75 | FL.NEW_78 | FL.EDITED_78 | FL.NEW_78B1 | FL.EDITED_78B1 | FL.NEW_W1 | FL.EDITED_W1                     # Stage 5.4.1 / the 6.1.1 CSS fix, each pinned by its own F tests\n',
         '    later = FL.NEW_541 | FL.EDITED_541 | FL.NEW_611FIX | FL.EDITED_611FIX | FL.NEW_OPS | FL.EDITED_OPS | FL.NEW_610D | FL.EDITED_610D | FL.NEW_542 | FL.EDITED_542 | FL.NEW_70 | FL.EDITED_70 | FL.NEW_71 | FL.EDITED_71 | FL.NEW_72 | FL.EDITED_72 | FL.NEW_73 | FL.EDITED_73 | FL.NEW_731 | FL.EDITED_731 | FL.NEW_74 | FL.EDITED_74 | FL.NEW_75 | FL.EDITED_75 | FL.NEW_78 | FL.EDITED_78 | FL.NEW_78B1 | FL.EDITED_78B1 | FL.NEW_W1 | FL.EDITED_W1 | FL.NEW_W2A | FL.EDITED_W2A                     # Stage 5.4.1 / the 6.1.1 CSS fix, each pinned by its own F tests\n'),
        ("            assert now_text(rel) == post(rel) or rel in FL.SEMANTIC_541 or rel in FL.SEMANTIC_W1, rel   # since then: only Stage 5.4.1's and Wave 1's declared corrections\n",
         "            assert now_text(rel) == post(rel) or rel in FL.SEMANTIC_541 or rel in FL.SEMANTIC_W1 or rel in FL.SEMANTIC_W2A, rel   # since then: only Stage 5.4.1's and Wave 1's declared corrections\n"),
    ],
    'tests/test_dynamic_universe_610c.py': [
        ('from freeze_ledger import SEMANTIC_W1  # noqa: E402 - Wave 1: its declared execution-safety files (tests/freeze_ledger.py)\n',
         'from freeze_ledger import SEMANTIC_W1, SEMANTIC_W2A  # noqa: E402 - Wave 1: its declared execution-safety files (tests/freeze_ledger.py)\n'),
        ('        assert base and ((ROOT / rel).read_text(encoding="utf-8").replace("\\r\\n", "\\n") == base or rel in SEMANTIC_W1), rel   # Wave 1: its declared stop-check change, no universe wiring\n',
         '        assert base and ((ROOT / rel).read_text(encoding="utf-8").replace("\\r\\n", "\\n") == base or rel in SEMANTIC_W1 or rel in SEMANTIC_W2A), rel   # Wave 1: its declared stop-check change, no universe wiring\n'),
    ],
    'tests/test_engine_health_610d.py': [
        ('    later = FL.NEW_542 | FL.EDITED_542 | FL.NEW_70 | FL.EDITED_70 | FL.NEW_71 | FL.EDITED_71 | FL.NEW_72 | FL.EDITED_72 | FL.NEW_73 | FL.EDITED_73 | FL.NEW_731 | FL.EDITED_731 | FL.NEW_74 | FL.EDITED_74 | FL.NEW_75 | FL.EDITED_75 | FL.NEW_78 | FL.EDITED_78 | FL.NEW_78B1 | FL.EDITED_78B1 | FL.NEW_W1 | FL.EDITED_W1                                                      # Stage 5.4.2, pinned by its own F tests\n',
         '    later = FL.NEW_542 | FL.EDITED_542 | FL.NEW_70 | FL.EDITED_70 | FL.NEW_71 | FL.EDITED_71 | FL.NEW_72 | FL.EDITED_72 | FL.NEW_73 | FL.EDITED_73 | FL.NEW_731 | FL.EDITED_731 | FL.NEW_74 | FL.EDITED_74 | FL.NEW_75 | FL.EDITED_75 | FL.NEW_78 | FL.EDITED_78 | FL.NEW_78B1 | FL.EDITED_78B1 | FL.NEW_W1 | FL.EDITED_W1 | FL.NEW_W2A | FL.EDITED_W2A                                                      # Stage 5.4.2, pinned by its own F tests\n'),
        ('            assert p.read_text(encoding="utf-8").replace("\\r\\n", "\\n") == at_base(rel) or rel in FL.SEMANTIC_W1, rel   # Wave 1\'s declared safety files: its own F tests\n',
         '            assert rel in FL.NEW_W2A or rel in FL.SEMANTIC_W2A or p.read_text(encoding="utf-8").replace("\\r\\n", "\\n") == at_base(rel) or rel in FL.SEMANTIC_W1, rel   # Wave 1\'s declared safety files: its own F tests\n'),
    ],
    'tests/test_evidence_gate_541.py': [
        ('    later = FL.NEW_611FIX | FL.EDITED_611FIX | FL.NEW_OPS | FL.EDITED_OPS | FL.NEW_610D | FL.EDITED_610D | FL.NEW_542 | FL.EDITED_542 | FL.NEW_70 | FL.EDITED_70 | FL.NEW_71 | FL.EDITED_71 | FL.NEW_72 | FL.EDITED_72 | FL.NEW_73 | FL.EDITED_73 | FL.NEW_731 | FL.EDITED_731 | FL.NEW_74 | FL.EDITED_74 | FL.NEW_75 | FL.EDITED_75 | FL.NEW_78 | FL.EDITED_78 | FL.NEW_78B1 | FL.EDITED_78B1 | FL.NEW_W1 | FL.EDITED_W1                                                  # the Stage 6.1.1 CSS fix, pinned by its own F tests\n',
         '    later = FL.NEW_611FIX | FL.EDITED_611FIX | FL.NEW_OPS | FL.EDITED_OPS | FL.NEW_610D | FL.EDITED_610D | FL.NEW_542 | FL.EDITED_542 | FL.NEW_70 | FL.EDITED_70 | FL.NEW_71 | FL.EDITED_71 | FL.NEW_72 | FL.EDITED_72 | FL.NEW_73 | FL.EDITED_73 | FL.NEW_731 | FL.EDITED_731 | FL.NEW_74 | FL.EDITED_74 | FL.NEW_75 | FL.EDITED_75 | FL.NEW_78 | FL.EDITED_78 | FL.NEW_78B1 | FL.EDITED_78B1 | FL.NEW_W1 | FL.EDITED_W1 | FL.NEW_W2A | FL.EDITED_W2A                                                  # the Stage 6.1.1 CSS fix, pinned by its own F tests\n'),
        ('            assert p.read_text(encoding="utf-8").replace("\\r\\n", "\\n") == post(rel) or rel in FL.SEMANTIC_W1, rel   # since then: Stage 6.10D touches none of these; Wave 1 only its declared safety files\n',
         '            assert p.read_text(encoding="utf-8").replace("\\r\\n", "\\n") == post(rel) or rel in FL.SEMANTIC_W1 or rel in FL.SEMANTIC_W2A, rel   # since then: Stage 6.10D touches none of these; Wave 1 only its declared safety files\n'),
    ],
    'tests/test_evidence_refresh_542.py': [
        ('    later = FL.NEW_70 | FL.EDITED_70 | FL.NEW_71 | FL.EDITED_71 | FL.NEW_72 | FL.EDITED_72 | FL.NEW_73 | FL.EDITED_73 | FL.NEW_731 | FL.EDITED_731 | FL.NEW_74 | FL.EDITED_74 | FL.NEW_75 | FL.EDITED_75 | FL.NEW_78 | FL.EDITED_78 | FL.NEW_78B1 | FL.EDITED_78B1 | FL.NEW_W1 | FL.EDITED_W1                                                         # Stage 7.0, pinned by its own F tests\n',
         '    later = FL.NEW_70 | FL.EDITED_70 | FL.NEW_71 | FL.EDITED_71 | FL.NEW_72 | FL.EDITED_72 | FL.NEW_73 | FL.EDITED_73 | FL.NEW_731 | FL.EDITED_731 | FL.NEW_74 | FL.EDITED_74 | FL.NEW_75 | FL.EDITED_75 | FL.NEW_78 | FL.EDITED_78 | FL.NEW_78B1 | FL.EDITED_78B1 | FL.NEW_W1 | FL.EDITED_W1 | FL.NEW_W2A | FL.EDITED_W2A                                                         # Stage 7.0, pinned by its own F tests\n'),
    ],
    'tests/test_forward_paper_610a.py': [
        ('    later = FL.NEW_610C | FL.EDITED_610C | FL.NEW_541 | FL.EDITED_541 | FL.NEW_611FIX | FL.EDITED_611FIX | FL.NEW_OPS | FL.EDITED_OPS | FL.NEW_610D | FL.EDITED_610D | FL.NEW_542 | FL.EDITED_542 | FL.NEW_70 | FL.EDITED_70 | FL.NEW_71 | FL.EDITED_71 | FL.NEW_72 | FL.EDITED_72 | FL.NEW_73 | FL.EDITED_73 | FL.NEW_731 | FL.EDITED_731 | FL.NEW_74 | FL.EDITED_74 | FL.NEW_75 | FL.EDITED_75 | FL.NEW_78 | FL.EDITED_78 | FL.NEW_78B1 | FL.EDITED_78B1 | FL.NEW_W1 | FL.EDITED_W1   # Stage 6.10C / 5.4.1 / the 6.1.1 CSS fix, each pinned by its own F tests\n',
         '    later = FL.NEW_610C | FL.EDITED_610C | FL.NEW_541 | FL.EDITED_541 | FL.NEW_611FIX | FL.EDITED_611FIX | FL.NEW_OPS | FL.EDITED_OPS | FL.NEW_610D | FL.EDITED_610D | FL.NEW_542 | FL.EDITED_542 | FL.NEW_70 | FL.EDITED_70 | FL.NEW_71 | FL.EDITED_71 | FL.NEW_72 | FL.EDITED_72 | FL.NEW_73 | FL.EDITED_73 | FL.NEW_731 | FL.EDITED_731 | FL.NEW_74 | FL.EDITED_74 | FL.NEW_75 | FL.EDITED_75 | FL.NEW_78 | FL.EDITED_78 | FL.NEW_78B1 | FL.EDITED_78B1 | FL.NEW_W1 | FL.EDITED_W1 | FL.NEW_W2A | FL.EDITED_W2A   # Stage 6.10C / 5.4.1 / the 6.1.1 CSS fix, each pinned by its own F tests\n'),
        ("        assert not [l for l in post(rel).splitlines() if l not in set(FL.undo_w1(rel, now_text(rel)).splitlines())], rel   # a later stage keeps every line Stage 6.10A left (Wave 1's declared config.py edit undone first)\n",
         "        assert not [l for l in post(rel).splitlines() if l not in set(FL.undo_w1(rel, FL.pre_w2a(rel, now_text(rel))).splitlines())], rel   # a later stage keeps every line Stage 6.10A left (Wave 1's declared config.py edit undone first)\n"),
    ],
    'tests/test_market_intel_70.py': [
        ('    later = FL.NEW_71 | FL.EDITED_71 | FL.NEW_72 | FL.EDITED_72 | FL.NEW_73 | FL.EDITED_73 | FL.NEW_731 | FL.EDITED_731 | FL.NEW_74 | FL.EDITED_74 | FL.NEW_75 | FL.EDITED_75 | FL.NEW_78 | FL.EDITED_78 | FL.NEW_78B1 | FL.EDITED_78B1 | FL.NEW_W1 | FL.EDITED_W1   # Stages 7.1-7.3: pinned by their own F tests\n',
         '    later = FL.NEW_71 | FL.EDITED_71 | FL.NEW_72 | FL.EDITED_72 | FL.NEW_73 | FL.EDITED_73 | FL.NEW_731 | FL.EDITED_731 | FL.NEW_74 | FL.EDITED_74 | FL.NEW_75 | FL.EDITED_75 | FL.NEW_78 | FL.EDITED_78 | FL.NEW_78B1 | FL.EDITED_78B1 | FL.NEW_W1 | FL.EDITED_W1 | FL.NEW_W2A | FL.EDITED_W2A   # Stages 7.1-7.3: pinned by their own F tests\n'),
    ],
    'tests/test_market_intel_collector_73.py': [
        ('    later = FL.NEW_731 | FL.EDITED_731 | FL.NEW_74 | FL.EDITED_74 | FL.NEW_75 | FL.EDITED_75 | FL.NEW_78 | FL.EDITED_78 | FL.NEW_78B1 | FL.EDITED_78B1 | FL.NEW_W1 | FL.EDITED_W1                           # Stages 7.3.1, 7.4: pinned by their own F tests\n',
         '    later = FL.NEW_731 | FL.EDITED_731 | FL.NEW_74 | FL.EDITED_74 | FL.NEW_75 | FL.EDITED_75 | FL.NEW_78 | FL.EDITED_78 | FL.NEW_78B1 | FL.EDITED_78B1 | FL.NEW_W1 | FL.EDITED_W1 | FL.NEW_W2A | FL.EDITED_W2A                           # Stages 7.3.1, 7.4: pinned by their own F tests\n'),
    ],
    'tests/test_market_intel_quality_74.py': [
        ('    later = FL.NEW_75 | FL.EDITED_75 | FL.NEW_78 | FL.EDITED_78 | FL.NEW_78B1 | FL.EDITED_78B1 | FL.NEW_W1 | FL.EDITED_W1                                                         # Stage 7.5, pinned by its own F tests\n',
         '    later = FL.NEW_75 | FL.EDITED_75 | FL.NEW_78 | FL.EDITED_78 | FL.NEW_78B1 | FL.EDITED_78B1 | FL.NEW_W1 | FL.EDITED_W1 | FL.NEW_W2A | FL.EDITED_W2A                                                         # Stage 7.5, pinned by its own F tests\n'),
    ],
    'tests/test_market_intel_schema_v2_731.py': [
        ('    later = FL.NEW_74 | FL.EDITED_74 | FL.NEW_75 | FL.EDITED_75 | FL.NEW_78 | FL.EDITED_78 | FL.NEW_78B1 | FL.EDITED_78B1 | FL.NEW_W1 | FL.EDITED_W1                                                         # Stage 7.4, pinned by its own F tests\n',
         '    later = FL.NEW_74 | FL.EDITED_74 | FL.NEW_75 | FL.EDITED_75 | FL.NEW_78 | FL.EDITED_78 | FL.NEW_78B1 | FL.EDITED_78B1 | FL.NEW_W1 | FL.EDITED_W1 | FL.NEW_W2A | FL.EDITED_W2A                                                         # Stage 7.4, pinned by its own F tests\n'),
    ],
    'tests/test_market_intel_sources_72.py': [
        ('    later = FL.NEW_73 | FL.EDITED_73 | FL.NEW_731 | FL.EDITED_731 | FL.NEW_74 | FL.EDITED_74 | FL.NEW_75 | FL.EDITED_75 | FL.NEW_78 | FL.EDITED_78 | FL.NEW_78B1 | FL.EDITED_78B1 | FL.NEW_W1 | FL.EDITED_W1                           # Stages 7.3, 7.3.1: pinned by their own F tests\n',
         '    later = FL.NEW_73 | FL.EDITED_73 | FL.NEW_731 | FL.EDITED_731 | FL.NEW_74 | FL.EDITED_74 | FL.NEW_75 | FL.EDITED_75 | FL.NEW_78 | FL.EDITED_78 | FL.NEW_78B1 | FL.EDITED_78B1 | FL.NEW_W1 | FL.EDITED_W1 | FL.NEW_W2A | FL.EDITED_W2A                           # Stages 7.3, 7.3.1: pinned by their own F tests\n'),
    ],
    'tests/test_market_intel_store_71.py': [
        ('    later = FL.NEW_72 | FL.EDITED_72 | FL.NEW_73 | FL.EDITED_73 | FL.NEW_731 | FL.EDITED_731 | FL.NEW_74 | FL.EDITED_74 | FL.NEW_75 | FL.EDITED_75 | FL.NEW_78 | FL.EDITED_78 | FL.NEW_78B1 | FL.EDITED_78B1 | FL.NEW_W1 | FL.EDITED_W1                             # Stages 7.2, 7.3: pinned by their own F tests\n',
         '    later = FL.NEW_72 | FL.EDITED_72 | FL.NEW_73 | FL.EDITED_73 | FL.NEW_731 | FL.EDITED_731 | FL.NEW_74 | FL.EDITED_74 | FL.NEW_75 | FL.EDITED_75 | FL.NEW_78 | FL.EDITED_78 | FL.NEW_78B1 | FL.EDITED_78B1 | FL.NEW_W1 | FL.EDITED_W1 | FL.NEW_W2A | FL.EDITED_W2A                             # Stages 7.2, 7.3: pinned by their own F tests\n'),
    ],
    'tests/test_market_scan_75.py': [
        ('    later = FL.NEW_78 | FL.EDITED_78 | FL.NEW_78B1 | FL.EDITED_78B1 | FL.NEW_W1 | FL.EDITED_W1                           # Stages 7.8, 7.8B-1: pinned by their own F tests\n',
         '    later = FL.NEW_78 | FL.EDITED_78 | FL.NEW_78B1 | FL.EDITED_78B1 | FL.NEW_W1 | FL.EDITED_W1 | FL.NEW_W2A | FL.EDITED_W2A                           # Stages 7.8, 7.8B-1: pinned by their own F tests\n'),
    ],
    'tests/test_robinhood_portfolio_69.py': [
        ('    later = FL.NEW_691 | FL.EDITED_691 | FL.NEW_610A | FL.EDITED_610A | FL.NEW_610C | FL.EDITED_610C | FL.NEW_541 | FL.EDITED_541 | FL.NEW_611FIX | FL.EDITED_611FIX | FL.NEW_OPS | FL.EDITED_OPS | FL.NEW_610D | FL.EDITED_610D | FL.NEW_542 | FL.EDITED_542 | FL.NEW_70 | FL.EDITED_70 | FL.NEW_71 | FL.EDITED_71 | FL.NEW_72 | FL.EDITED_72 | FL.NEW_73 | FL.EDITED_73 | FL.NEW_731 | FL.EDITED_731 | FL.NEW_74 | FL.EDITED_74 | FL.NEW_75 | FL.EDITED_75 | FL.NEW_78 | FL.EDITED_78 | FL.NEW_78B1 | FL.EDITED_78B1 | FL.NEW_W1 | FL.EDITED_W1   # the 6.1.1 CSS fix, Stage 6.9.1 (FL.SEMANTIC_691), Stage 6.10A (FL.SEMANTIC_610A), Stage 6.10C, Stage 5.4.1 (FL.SEMANTIC_541),\n',
         '    later = FL.NEW_691 | FL.EDITED_691 | FL.NEW_610A | FL.EDITED_610A | FL.NEW_610C | FL.EDITED_610C | FL.NEW_541 | FL.EDITED_541 | FL.NEW_611FIX | FL.EDITED_611FIX | FL.NEW_OPS | FL.EDITED_OPS | FL.NEW_610D | FL.EDITED_610D | FL.NEW_542 | FL.EDITED_542 | FL.NEW_70 | FL.EDITED_70 | FL.NEW_71 | FL.EDITED_71 | FL.NEW_72 | FL.EDITED_72 | FL.NEW_73 | FL.EDITED_73 | FL.NEW_731 | FL.EDITED_731 | FL.NEW_74 | FL.EDITED_74 | FL.NEW_75 | FL.EDITED_75 | FL.NEW_78 | FL.EDITED_78 | FL.NEW_78B1 | FL.EDITED_78B1 | FL.NEW_W1 | FL.EDITED_W1 | FL.NEW_W2A | FL.EDITED_W2A   # the 6.1.1 CSS fix, Stage 6.9.1 (FL.SEMANTIC_691), Stage 6.10A (FL.SEMANTIC_610A), Stage 6.10C, Stage 5.4.1 (FL.SEMANTIC_541),\n'),
    ],
    'tests/test_runtime_reliability_ops.py': [
        ('    assert changed == FL.NEW_OPS | FL.EDITED_OPS | FL.NEW_610D | FL.EDITED_610D | FL.NEW_542 | FL.EDITED_542 | FL.NEW_70 | FL.EDITED_70 | FL.NEW_71 | FL.EDITED_71 | FL.NEW_72 | FL.EDITED_72 | FL.NEW_73 | FL.EDITED_73 | FL.NEW_731 | FL.EDITED_731 | FL.NEW_74 | FL.EDITED_74 | FL.NEW_75 | FL.EDITED_75 | FL.NEW_78 | FL.EDITED_78 | FL.NEW_78B1 | FL.EDITED_78B1 | FL.NEW_W1 | FL.EDITED_W1\n',
         '    assert changed == FL.NEW_OPS | FL.EDITED_OPS | FL.NEW_610D | FL.EDITED_610D | FL.NEW_542 | FL.EDITED_542 | FL.NEW_70 | FL.EDITED_70 | FL.NEW_71 | FL.EDITED_71 | FL.NEW_72 | FL.EDITED_72 | FL.NEW_73 | FL.EDITED_73 | FL.NEW_731 | FL.EDITED_731 | FL.NEW_74 | FL.EDITED_74 | FL.NEW_75 | FL.EDITED_75 | FL.NEW_78 | FL.EDITED_78 | FL.NEW_78B1 | FL.EDITED_78B1 | FL.NEW_W1 | FL.EDITED_W1 | FL.NEW_W2A | FL.EDITED_W2A\n'),
    ],
    'tests/test_sip_eval_78b1.py': [
        ('    later = FL.NEW_W1 | FL.EDITED_W1                                                           # Wave 1, pinned by its own F tests\n',
         '    later = FL.NEW_W1 | FL.EDITED_W1 | FL.NEW_W2A | FL.EDITED_W2A                              # Wave 1 / Wave 2 PR A, pinned by their own F tests\n'),
    ],
    'tests/test_swing_pullback_78.py': [
        ('    later = FL.NEW_78B1 | FL.EDITED_78B1 | FL.NEW_W1 | FL.EDITED_W1                                                     # Stage 7.8B-1, pinned by its own F tests\n',
         '    later = FL.NEW_78B1 | FL.EDITED_78B1 | FL.NEW_W1 | FL.EDITED_W1 | FL.NEW_W2A | FL.EDITED_W2A                                                     # Stage 7.8B-1, pinned by its own F tests\n'),
    ],
    'tests/test_unified_exposure_gate_691.py': [
        ('    later = FL.NEW_610A | FL.EDITED_610A | FL.NEW_610C | FL.EDITED_610C | FL.NEW_541 | FL.EDITED_541 | FL.NEW_611FIX | FL.EDITED_611FIX | FL.NEW_OPS | FL.EDITED_OPS | FL.NEW_610D | FL.EDITED_610D | FL.NEW_542 | FL.EDITED_542 | FL.NEW_70 | FL.EDITED_70 | FL.NEW_71 | FL.EDITED_71 | FL.NEW_72 | FL.EDITED_72 | FL.NEW_73 | FL.EDITED_73 | FL.NEW_731 | FL.EDITED_731 | FL.NEW_74 | FL.EDITED_74 | FL.NEW_75 | FL.EDITED_75 | FL.NEW_78 | FL.EDITED_78 | FL.NEW_78B1 | FL.EDITED_78B1 | FL.NEW_W1 | FL.EDITED_W1   # Stage 6.10A / 6.10C / 5.4.1 / the 6.1.1 CSS fix, each pinned by its own F tests\n',
         '    later = FL.NEW_610A | FL.EDITED_610A | FL.NEW_610C | FL.EDITED_610C | FL.NEW_541 | FL.EDITED_541 | FL.NEW_611FIX | FL.EDITED_611FIX | FL.NEW_OPS | FL.EDITED_OPS | FL.NEW_610D | FL.EDITED_610D | FL.NEW_542 | FL.EDITED_542 | FL.NEW_70 | FL.EDITED_70 | FL.NEW_71 | FL.EDITED_71 | FL.NEW_72 | FL.EDITED_72 | FL.NEW_73 | FL.EDITED_73 | FL.NEW_731 | FL.EDITED_731 | FL.NEW_74 | FL.EDITED_74 | FL.NEW_75 | FL.EDITED_75 | FL.NEW_78 | FL.EDITED_78 | FL.NEW_78B1 | FL.EDITED_78B1 | FL.NEW_W1 | FL.EDITED_W1 | FL.NEW_W2A | FL.EDITED_W2A   # Stage 6.10A / 6.10C / 5.4.1 / the 6.1.1 CSS fix, each pinned by its own F tests\n'),
        ("        assert not [l for l in post(rel).splitlines() if l not in set(FL.undo_w1(rel, _now(rel)).splitlines())], rel   # Stage 6.10A keeps every line Stage 6.9.1 left (Wave 1's declared config.py edit undone first)\n",
         "        assert not [l for l in post(rel).splitlines() if l not in set(FL.undo_w1(rel, FL.pre_w2a(rel, _now(rel))).splitlines())], rel   # Stage 6.10A keeps every line Stage 6.9.1 left (Wave 1's declared config.py edit undone first)\n"),
    ],
    'tests/test_wave1_safety_remediation.py': [
        ('APP_EDITS_W1 = {".github/ci/pytest_groups.json", "config.py", "execution/service.py", "safety/service.py", "safety/store.py"}   # the CI entry and the four fixed files\n',
         'def post(rel):\n    """main after PR #50 (Wave 1 merged; the Wave 2 base): what Wave 1 left, whatever a later change made since."""\n    run = subprocess.run(["git", "show", f"{FL.BASE_W2A}:{rel}"], cwd=ROOT, capture_output=True)\n    assert run.returncode == 0, f"{rel} is not in {FL.BASE_W2A} (fetch-depth 0 is required)"\n    return run.stdout.decode("utf-8").replace("\\r\\n", "\\n")\n\n\nAPP_EDITS_W1 ={".github/ci/pytest_groups.json", "config.py", "execution/service.py", "safety/service.py", "safety/store.py"}   # the CI entry and the four fixed files\n'),
        ('',
         '    stage = set(_git("diff", "--name-only", FL.BASE_W1, FL.BASE_W2A).split())                # Wave 1 itself, between two fixed commits (main before / after PR #50)\n'),
        ('    assert changed == FL.NEW_W1 | FL.EDITED_W1, {"unexpected": sorted(changed - FL.NEW_W1 - FL.EDITED_W1), "missing": sorted((FL.NEW_W1 | FL.EDITED_W1) - changed)}\n',
         '    later = FL.NEW_W2A | FL.EDITED_W2A                                                         # Wave 2 PR A, pinned by its own F tests\n    assert stage == FL.NEW_W1 | FL.EDITED_W1, {"unexpected": sorted(stage - FL.NEW_W1 - FL.EDITED_W1), "missing": sorted((FL.NEW_W1 | FL.EDITED_W1) - stage)}\n    assert changed == FL.NEW_W1 | FL.EDITED_W1 | later, {"unexpected": sorted(changed - FL.NEW_W1 - FL.EDITED_W1 - later), "missing": sorted((FL.NEW_W1 | FL.EDITED_W1 | later) - changed)}\n'),
        ('    assert {p for p in changed if p not in in_base} == FL.NEW_W1 and {p for p in changed if p in in_base} == FL.EDITED_W1   # so every other file of main is untouched\n',
         '    assert {p for p in stage if p not in in_base} == FL.NEW_W1 and {p for p in stage if p in in_base} == FL.EDITED_W1   # so every other file of main is untouched\n'),
        ('        assert not [l for l in now_text(rel).splitlines() if l not in base and marker.match(l)], rel\n',
         '        assert not [l for l in post(rel).splitlines() if l not in base and marker.match(l)], rel\n'),
        ('    svc, svc_base = now_text("execution/service.py"), at_base("execution/service.py")\n',
         '    svc, svc_base = FL.pre_w2a("execution/service.py", now_text("execution/service.py")), at_base("execution/service.py")   # Wave 2 PR A\'s accounting change: its own tests\n'),
        ('        text = now_text(rel)\n',
         "        text = post(rel)                                                                       # Wave 1's edits as main left them (Wave 2 PR A extends some lines: its own F03)\n"),
        ('    assert set(REPLAY_W1) == FL.EDITED_W1 - set(FL.LEDGER) - {"tests/freeze_ledger.py"}       # every other edited file carries a ledger entry\n    fl, base = now_text("tests/freeze_ledger.py"), at_base("tests/freeze_ledger.py")\n',
         '    fl, base = post("tests/freeze_ledger.py"), at_base("tests/freeze_ledger.py")             # the ledger as Wave 1 left it (Wave 2 PR A\'s additions: its own F03)\n    main_ledger = ast.literal_eval(fl[fl.index("LEDGER = {"):fl.index("}\\n", fl.index("LEDGER = {")) + 2][len("LEDGER = "):])\n    assert set(REPLAY_W1) == FL.EDITED_W1 - set(main_ledger) - {"tests/freeze_ledger.py"}      # every other edited file carries a ledger entry\n'),
        ('    assert set(FL.LEDGER) - set(old) == NEW_LEDGER_W1 and set(old) <= set(FL.LEDGER)\n    assert {k for k in old if old[k] != FL.LEDGER[k]} == FL.EDITED_W1 & set(old)              # every re-hash is a declared Wave 1 edit\n',
         '    assert set(main_ledger) - set(old) == NEW_LEDGER_W1 and set(old) <= set(main_ledger)\n    assert {k for k in old if old[k] != main_ledger[k]} == FL.EDITED_W1 & set(old)            # every re-hash is a declared Wave 1 edit\n'),
    ],
}
