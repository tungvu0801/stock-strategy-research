"""
EX-06 (the local request gate): the freeze pins, as every stage since 6.8. The change set is exactly NEW_EX06 + EDITED_EX06 (BASE_EX06 = main
after PR #53); the production diff is exactly the declared lines (T-X20); every changed older test function is declared (F02X); every
bookkeeping edit is undone exactly and the ledger changes only the declared entries (F03X). tests/test_ex06_local_gate.py pins the behaviour.
"""
import ast
import difflib
import json
import re
import subprocess
from pathlib import Path

import freeze_ledger as FL

ROOT = Path(__file__).resolve().parents[1]
SCRATCH = ("scratchpad/", "runtime/", "data/market_intel", "logs/", "browser_tests/artifacts/")
NEW_FILES_EX06 = {"api/local_gate.py", "DESIGN_EX06_LOCAL_REQUEST_GATE.md", "tests/test_ex06_local_gate.py", "tests/test_ex06_freeze.py",
                  "tests/frontend/ex06_local_intent.test.cjs"}
CI_FILES_EX06 = ("test_ex06_local_gate.py", "test_ex06_freeze.py")
PROD_EDITS_EX06 = {                                                                      # file -> (main's removed lines, EX-06's added lines), exactly
    "api/server.py": ([], ['    docs_url=None, redoc_url=None, openapi_url=None,                     # EX-06 (D-16): no Swagger / ReDoc page loading CDN scripts into this origin',
                           'from api.local_gate import LocalRequestGate  # noqa: E402 - EX-06',
                           'app.add_middleware(LocalRequestGate)                                    # EX-06 (D-15): the outermost middleware, so it must be added LAST']),
    "browser_tests/flows.py": (['                                     headers={"Content-Type": "application/json"})'],
                               ['                                     headers={"Content-Type": "application/json", "X-Stock-Agent-Intent": "dashboard"})']),
    "scripts/stock_agent_common.ps1": (['    return Invoke-RestMethod -Uri ("http://127.0.0.1:{0}{1}" -f $Port, $Path) -Method Get -TimeoutSec $TimeoutSec -UseBasicParsing'],
                                       ['    return Invoke-RestMethod -Uri ("http://127.0.0.1:{0}{1}" -f $Port, $Path) -Method Get -TimeoutSec $TimeoutSec -UseBasicParsing -Headers @{ "X-Stock-Agent-Intent" = "dashboard" }   # EX-06: the local request gate']),
    "scripts/start_market_morning.ps1": (['        try { $act = Invoke-RestMethod -Uri ("http://127.0.0.1:{0}/api/execution/activity" -f $Port) -Method Get -TimeoutSec 60 -UseBasicParsing } catch { $act = $null }'],
                                         ['        try { $act = Invoke-RestMethod -Uri ("http://127.0.0.1:{0}/api/execution/activity" -f $Port) -Method Get -TimeoutSec 60 -UseBasicParsing -Headers @{ "X-Stock-Agent-Intent" = "dashboard" } } catch { $act = $null }   # EX-06: the local request gate']),
    "scripts/run_aapl_paper_smoke.py": (['            with urllib.request.urlopen(url.rstrip("/") + "/api/health", timeout=10) as r:',
                                         '            with urllib.request.urlopen(url.rstrip("/") + "/api/execution/status", timeout=20) as r:'],
                                        ['            with urllib.request.urlopen(urllib.request.Request(url.rstrip("/") + "/api/health", headers={"X-Stock-Agent-Intent": "dashboard"}), timeout=10) as r:   # EX-06',
                                         '            with urllib.request.urlopen(urllib.request.Request(url.rstrip("/") + "/api/execution/status", headers={"X-Stock-Agent-Intent": "dashboard"}), timeout=20) as r:   # EX-06']),
}
APP_JS = "frontend/app.js"
BLOCK_BEGIN, BLOCK_END, BLOCK_BEFORE = "// EX-06 local intent: begin", "// EX-06 local intent: end", "const REFRESH_MS = 30000;"
PROD_EX06 = set(PROD_EDITS_EX06) | {APP_JS}
FORBIDDEN_PREFIX_EX06 = ("execution/", "safety/", "autorun/", "forward_validation/", "forward_capture/", "strategy_registry/", "setups/", "proposals/", "paper/",
                         "unified_portfolio/", "cash_flow/", "account_refresh/", "market_intel/", "research_data/", "swing_eval/", "swing_pullback/", "api/routes/")
HELPERS_EX06 = '''
def pre_ex06(rel: str, text: str) -> str:
    if rel not in SEMANTIC_EX06:
        return text
    out = subprocess.run(["git", "show", f"{BASE_EX06}:{rel}"], cwd=ROOT, capture_output=True)
    assert out.returncode == 0, f"{rel} is not in {BASE_EX06} (fetch-depth 0 is required)"
    return _norm(out.stdout.decode("utf-8"))


def undo_ex06(rel: str, text: str) -> str:
    text = _norm(text)
    for old, new in reversed(UNDO_EX06.get(rel, [])):
        assert text.count(new) == 1, (rel, new[:80])
        text = text.replace(new, old)
    return text
'''                                                                                         # the two helpers' code, exactly (docstrings aside)


def _git(*a):
    return subprocess.run(["git", *a], cwd=ROOT, capture_output=True, text=True).stdout


def at_base(rel):
    run = subprocess.run(["git", "show", f"{FL.BASE_EX06}:{rel}"], cwd=ROOT, capture_output=True)
    assert run.returncode == 0, f"{rel} is not in {FL.BASE_EX06} (fetch-depth 0 is required)"
    return run.stdout.decode("utf-8").replace("\r\n", "\n")


def now_text(rel):
    return (ROOT / rel).read_text(encoding="utf-8").replace("\r\n", "\n")


def changed_files(base):
    c = set(_git("diff", "--name-only", base).split()) | set(_git("ls-files", "--others", "--exclude-standard").split())
    return {p for p in c if not p.startswith(SCRATCH) and (ROOT / p).exists()}


def ex06_files(base):
    """XSS-01: EX-06's change set between fixed commits (`base`..main after PR #55, FL.BASE_XSS01); XSS-01's own delta is pinned by tests/test_xss01_freeze.py."""
    return set(_git("diff", "--name-only", base, FL.BASE_XSS01).split())


def defs(text):
    out = {}
    for n in ast.parse(text).body:
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            out[n.name] = ast.dump(n)
        elif isinstance(n, (ast.Assign, ast.AnnAssign)):
            for t in (n.targets if isinstance(n, ast.Assign) else [n.target]):
                if isinstance(t, ast.Name):
                    out["=" + t.id] = ast.dump(n)
    return out


def test_TX20_the_production_diff_is_exactly_the_declared_lines_and_no_trading_file_changed():
    changed = ex06_files(FL.BASE_EX06)                                                          # XSS-01: EX-06's own change set (fixed commits)
    prod = {p for p in changed if not p.startswith("tests/") and p != ".github/ci/pytest_groups.json" and p not in NEW_FILES_EX06}
    assert prod == PROD_EX06, {"unexpected": sorted(prod - PROD_EX06), "missing": sorted(PROD_EX06 - prod)}
    for rel, (removed, added) in PROD_EDITS_EX06.items():
        base, now = at_base(rel).split("\n"), FL.pre_panel(rel, now_text(rel)).split("\n")   # risk controls panel: main's text (its own delta: tests/test_panel_freeze.py)
        ops = [(base[i1:i2], now[j1:j2]) for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(None, base, now, autojunk=False).get_opcodes() if tag != "equal"]
        assert [l for b, _ in ops for l in b] == removed and [l for _, n in ops for l in n] == added, (rel, ops)   # the exact edit, in order:
        assert len(now) == len(base) + len(added) - len(removed), rel                        # a moved or reordered line is a removal + an addition
    js, base_js = FL.pre_xss01(APP_JS, now_text(APP_JS)), at_base(APP_JS)                     # XSS-01: app.js as main left it after PR #55 (XSS-01's escaping: tests/test_xss01_freeze.py)
    a, b = js.index(BLOCK_BEGIN), js.index("\n", js.index(BLOCK_END)) + 1
    assert js[:a] + js[b:] == base_js and js[b:].startswith(BLOCK_BEFORE) and js.count(BLOCK_BEGIN) == js.count(BLOCK_END) == 1
    server = now_text("api/server.py")
    assert server.rindex("app.add_middleware(") == server.index("app.add_middleware(LocalRequestGate)")   # added LAST = outermost
    assert not {p for p in changed if p.startswith(FORBIDDEN_PREFIX_EX06)} and "config.py" not in changed and "frontend/index.html" not in changed
    gate = now_text("api/local_gate.py")
    assert not re.search(r"^\s*(import|from)\s+(subprocess|requests|urllib\.request|socket|paper|execution|safety|config)\b", gate, re.M)


def test_F01X_the_change_set_is_exactly_the_declared_files():
    assert FL.BASE_EX06 == "46ebc65671435a02a0c90f6cd40f7d277ab15da2" and subprocess.run(["git", "cat-file", "-e", f"{FL.BASE_EX06}^{{commit}}"], cwd=ROOT).returncode == 0
    changed = ex06_files(FL.BASE_EX06)                                                          # XSS-01: EX-06's own change set (fixed commits)
    assert changed == FL.NEW_EX06 | FL.EDITED_EX06, {"unexpected": sorted(changed - FL.NEW_EX06 - FL.EDITED_EX06), "missing": sorted((FL.NEW_EX06 | FL.EDITED_EX06) - changed)}
    in_base = set(_git("ls-tree", "-r", "--name-only", FL.BASE_EX06).split())
    assert {p for p in changed if p not in in_base} == FL.NEW_EX06 == NEW_FILES_EX06 and {p for p in changed if p in in_base} == FL.EDITED_EX06
    assert not _git("diff", "--name-only", "--diff-filter=D", FL.BASE_EX06).split()
    assert {p for p in FL.EDITED_EX06 if not p.startswith("tests/")} == PROD_EX06 | {".github/ci/pytest_groups.json"}
    assert set(FL.SEMANTIC_EX06) == PROD_EX06 and FL.INSTRUMENTATION_EX06 == {} and all(len(v) > 40 for v in FL.SEMANTIC_EX06.values())
    assert all(p.endswith((".py", ".cjs")) for p in FL.EDITED_EX06 if p.startswith("tests/"))
    marker = re.compile(r"^\s*(#|//|<!--)\s*(>>>|<<<) ")
    for rel in sorted(changed):
        base = set(at_base(rel).splitlines()) if rel in in_base else set()
        assert not [l for l in now_text(rel).splitlines() if l not in base and marker.match(l)], rel
    ex06_groups = subprocess.run(["git", "show", f"{FL.BASE_XSS01}:.github/ci/pytest_groups.json"], cwd=ROOT, capture_output=True).stdout.decode("utf-8")   # XSS-01: the manifest as PR #55 left it (XSS-01's entries: tests/test_xss01_freeze.py)
    groups, base_groups = json.loads(ex06_groups)["groups"], json.loads(at_base(".github/ci/pytest_groups.json"))["groups"]
    assert {g: v for g, v in groups.items() if g != "core"} == {g: v for g, v in base_groups.items() if g != "core"}
    i = base_groups["core"]["files"].index("test_ci_groups_603.py") + 1
    assert groups["core"] == {**base_groups["core"], "files": base_groups["core"]["files"][:i] + list(CI_FILES_EX06) + base_groups["core"]["files"][i:]}


def test_F02X_every_changed_older_test_function_is_declared():
    actual = set()
    for rel in sorted(p for p in FL.EDITED_EX06 if p.startswith("tests/") and p.endswith(".py") and p != "tests/freeze_ledger.py"):
        b, n = defs(at_base(rel)), defs(FL.undo_xss01(rel, FL.undo_panel(rel, now_text(rel))))                     # XSS-01: its own in-place edits undone first (tests/test_xss01_freeze.py)
        assert set(b) <= set(n), (rel, sorted(set(b) - set(n)))
        actual |= {f"{rel}::{k}" for k in set(n) if b.get(k) != n[k]}
    declared = set(FL.SEMANTIC_TESTS_EX06)
    assert actual == declared, {"undeclared": sorted(actual - declared), "declared_but_unchanged": sorted(declared - actual)}
    assert all(re.match(r"^EX06-[A-Z]\d\d ", v) and len(v) > 40 for v in FL.SEMANTIC_TESTS_EX06.values())
    for rel in FL.SEMANTIC_EX06:
        assert FL.pre_ex06(rel, now_text(rel)) == at_base(rel) and now_text(rel) != at_base(rel), rel
    assert FL.pre_ex06("config.py", "x") == "x" and FL.pre_ex06("api/local_gate.py", "y") == "y"


def test_F03X_every_bookkeeping_edit_is_pinned_exactly_and_the_ledger_changes_only_the_declared_entries():
    assert set(FL.UNDO_EX06) == {p for p in FL.EDITED_EX06 if p.startswith("tests/") and p.endswith((".py", ".cjs"))} - {"tests/freeze_ledger.py"}
    for rel in sorted(FL.UNDO_EX06):
        assert FL.undo_ex06(rel, FL.undo_xss01(rel, FL.undo_panel(rel, now_text(rel)))) == at_base(rel), rel       # now -> (XSS-01 undone) 9df1717a -> 46ebc656, byte for byte
    fl = subprocess.run(["git", "show", f"{FL.BASE_XSS01}:tests/freeze_ledger.py"], cwd=ROOT, capture_output=True).stdout.decode("utf-8").replace("\r\n", "\n")   # XSS-01: the ledger as PR #55 left it
    main_fl = at_base("tests/freeze_ledger.py")
    for a in ("LEDGER = {", "BASE_W2C = ", "BASE_EX06 = ", "\n\ndef unchanged(", "\n\ndef pre_w2c(", "\n\ndef pre_ex06(", "\nWave 2 PR C (design", "\nEX-06 ("):
        assert fl.count(a) == 1, a
    led = lambda t: t[t.index("\nLEDGER = {\n") + 1:t.index("}\n", t.index("\nLEDGER = {\n")) + 2]  # noqa: E731
    main_ledger = ast.literal_eval(led(main_fl)[len("LEDGER = "):])
    region = ast.parse(fl[fl.index("BASE_EX06 = "):fl.index("\nLEDGER = {\n") + 1]).body
    assert all(isinstance(n, (ast.Assign, ast.AnnAssign)) for n in region)
    assert [ast.unparse(t) for n in region for t in (n.targets if isinstance(n, ast.Assign) else [n.target])] == [
        "BASE_EX06", "NEW_EX06", "EDITED_EX06", "SEMANTIC_EX06", "INSTRUMENTATION_EX06", "NEW_LEDGER_EX06", "UNDO_EX06", "SEMANTIC_TESTS_EX06"]
    code = lambda src: [ast.dump(ast.FunctionDef(name=f.name, args=f.args, body=[s for s in f.body if not (isinstance(s, ast.Expr) and isinstance(s.value, ast.Constant))],  # noqa: E731
                                                 decorator_list=f.decorator_list, returns=f.returns, type_params=[])) for f in ast.parse(src).body]
    assert code(fl[fl.index("\n\ndef pre_ex06("):fl.index("\n\ndef unchanged(")]) == code(HELPERS_EX06)
    start = fl.index("\nEX-06 (")
    rebuilt = fl.replace(fl[start:fl.index('"""', start)], "", 1).replace(" + NEW_W2C + NEW_EX06 plus this ledger.", " + NEW_W2C plus this ledger.", 1)
    rebuilt = rebuilt.replace(rebuilt[rebuilt.index("BASE_EX06 = "):rebuilt.index("\nLEDGER = {\n") + 1], "", 1)
    rebuilt = rebuilt.replace(rebuilt[rebuilt.index("\n\ndef pre_ex06("):rebuilt.index("\n\ndef unchanged(")], "", 1)
    rebuilt = rebuilt.replace(led(rebuilt), led(main_fl), 1)
    assert rebuilt == main_fl                                                                 # the paragraph, one line, the declarations, two helpers and the ledger
    assert set(FL.LEDGER) - set(main_ledger) == FL.NEW_LEDGER_EX06 | FL.NEW_LEDGER_XSS01 | FL.NEW_LEDGER_PANEL and set(main_ledger) <= set(FL.LEDGER)   # XSS-01: its registrations are its own pin (F05Y)
    ex06_ledger = ast.literal_eval(led(fl)[len("LEDGER = "):])                                # risk controls panel (design 7a): the ledger as PR #55 left it; later re-hashes are their stages' own pins
    assert {k for k in main_ledger if main_ledger[k] != ex06_ledger[k]} == FL.EDITED_EX06 & set(main_ledger)
    for rel in sorted(FL.NEW_LEDGER_EX06):
        assert FL.base_text(rel) == at_base(rel) == FL.undo_ex06(rel, FL.pre_ex06(rel, now_text(rel))), rel   # unchanged from BASE_68 to main
    for rel in FL.LEDGER:
        assert FL.view(rel, FL.now_text(rel)) == FL.base_text(rel), rel
    FL.UNDO_EX06["tests/_probe.py"] = [("y = 2\n", "y = 3\n")]
    try:
        assert FL.undo_ex06("tests/_probe.py", "x = 1\ny = 3\n") == "x = 1\ny = 2\n" and FL.undo_ex06("tests/other.py", "z\r\n") == "z\n"
    finally:
        del FL.UNDO_EX06["tests/_probe.py"]


# ---------------------------------------------------------------- the hardening (H-1..H-4): its own pin, from main 9c9e9f5c (PR #54 merged)
BASE_EX06H = "9c9e9f5c2c2e946ed1a81d7369445a31355f8983"                                  # main after PR #54: the hardening base
FILES_EX06H = {"api/local_gate.py", "tests/test_ex06_local_gate.py", "tests/test_ex06_freeze.py", "DESIGN_EX06_LOCAL_REQUEST_GATE.md"}
GATE_REMOVED_EX06H = [                                                                   # main's lines, in order (T-X20's SequenceMatcher form)
    '  9. every /api GET / HEAD (D-14: any of them may persist) needs the dashboard intent header or a browser-asserted same-origin / user-typed',
    '     request (Sec-Fetch-Site same-origin / none); a header-less, metadata-less request is refused (old browsers, curl, scripts);',
    'never handed to the app.',
    'CODES = ("CLIENT", "HOST", "PROXY", "ORIGIN", "REFERER", "FETCH_SITE", "INTENT", "CONTENT_TYPE", "ERROR")',
    '        return None if (intent or site in SAME_ORIGIN_SITES) else _refuse("INTENT")',
]
GATE_ADDED_EX06H = [                                                                     # the hardening's lines, in order
    '  9. every /api GET / HEAD (D-14: any of them may persist) needs the dashboard intent header, or strict same-origin browser evidence (H-1):',
    '     Sec-Fetch-Site same-origin with exactly one Sec-Fetch-Mode and one Sec-Fetch-Dest, the Host with the bound port, and exactly one Referer',
    '     (and any Origin) whose origin is exactly http://<that Host>; a typed, bookmarked or externally opened /api URL (Sec-Fetch-Site none), a',
    '     header-less request (curl, scripts, old browsers) or incomplete metadata is refused;',
    'never handed to the app. A non-empty ASGI root_path (a mounted or prefixed deployment: unsupported, D-12) refuses every request and handshake',
    '(LOCAL_GATE_ROOT_PATH, H-4); lifespan is unaffected.',
    'CODES = ("CLIENT", "HOST", "PROXY", "ORIGIN", "REFERER", "FETCH_SITE", "INTENT", "CONTENT_TYPE", "ERROR", "ROOT_PATH")',
    'def _same_origin_read(host: str, port: int, site, modes: List[str], dests: List[str], origin: List[str], headers) -> bool:',
    '    """H-1: an /api read without the intent header passes only on complete browser fetch metadata asserting THIS origin: Sec-Fetch-Site',
    '    same-origin (never none: a typed, bookmarked or externally opened URL), exactly one Sec-Fetch-Mode and one Sec-Fetch-Dest, the Host with',
    '    the bound port (no portless name), and exactly one Referer (and any Origin) whose origin is exactly http://<that Host>."""',
    '    if site != "same-origin" or len(modes) != 1 or len(dests) != 1 or host not in allowed_hosts(port):',
    '        return False',
    '    ref = _values(headers, b"referer")',
    '    if len(ref) != 1:',
    '        return False',
    '    p = urlsplit(ref[0])',
    '    own = "http://" + host',
    '    return f"{p.scheme}://{p.netloc}".lower() == own and (not origin or origin[0].lower() == own)',
    '',
    '',
    '    if scope.get("root_path") not in (None, ""):',
    '        return _refuse("ROOT_PATH")                                                     # H-4: a mounted / prefixed deployment is unsupported (D-12)',
    '        return None if (intent or _same_origin_read(host[0].lower(), port, site, modes, dests, origin, headers)) else _refuse("INTENT")',
]


def test_TX21_the_hardening_changes_exactly_its_declared_files_and_the_gates_exact_lines():
    """H-1..H-4 (the independent review of PR #54: LOW-1, I-a, I-b, I-c). The change since BASE_EX06H is exactly EX-06's own four files (all in
    NEW_EX06, so T-X20 / F01X-F03X and every older freeze guard are unaffected: freeze-neutral), with no deletion; api/local_gate.py's diff is
    exactly the declared lines, in order, with the line count checked (a moved or reordered line is a removal plus an addition); no other
    production file changed; the helper is defined once and called once, and the root_path check comes before every other rule."""
    assert subprocess.run(["git", "cat-file", "-e", f"{BASE_EX06H}^{{commit}}"], cwd=ROOT).returncode == 0, "fetch-depth 0 is required"
    changed = ex06_files(BASE_EX06H)                                                            # XSS-01: the hardening's own change set (fixed commits)
    assert changed == FILES_EX06H, {"unexpected": sorted(changed - FILES_EX06H), "missing": sorted(FILES_EX06H - changed)}
    assert FILES_EX06H <= FL.NEW_EX06 and not _git("diff", "--name-only", "--diff-filter=D", BASE_EX06H).split()
    run = subprocess.run(["git", "show", f"{BASE_EX06H}:api/local_gate.py"], cwd=ROOT, capture_output=True)
    assert run.returncode == 0
    base, now = run.stdout.decode("utf-8").replace("\r\n", "\n").split("\n"), FL.pre_xss01("api/local_gate.py", now_text("api/local_gate.py")).split("\n")   # XSS-01: the gate as PR #55 left it (its CSP line: tests/test_xss01_freeze.py)
    ops = [(base[i1:i2], now[j1:j2]) for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(None, base, now, autojunk=False).get_opcodes() if tag != "equal"]
    assert [l for b, _ in ops for l in b] == GATE_REMOVED_EX06H and [l for _, n in ops for l in n] == GATE_ADDED_EX06H, ops
    assert len(now) == len(base) + len(GATE_ADDED_EX06H) - len(GATE_REMOVED_EX06H)
    gate = now_text("api/local_gate.py")
    assert gate.count("def _same_origin_read(") == 1 and gate.count("_same_origin_read(host[0].lower()") == 1
    assert gate.count('_refuse("ROOT_PATH")') == 1 and gate.index('_refuse("ROOT_PATH")') < gate.index("_loopback_client((scope")
