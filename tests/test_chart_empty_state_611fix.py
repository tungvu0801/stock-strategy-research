"""Stage 6.1.1 CSS fix (chart empty state): the overlay rules are scoped to `#pui-chart-empty .pui-chart-empty`, so the body state class
`pui-chart-empty` (set while the chart has no candles, e.g. market closed) no longer centres and narrows the whole page.

C  the CSS: four scoped overlay rules, no bare `.pui-chart-empty` rule, the body-state rules unchanged; the diff to main is exactly those four selectors
F  freeze: the change set since main (2fe29f3, Stage 5.4.1 merged) is exactly the declared files; every bookkeeping edit is pinned and replayed;
   the ledger re-hashes only the declared entries; no execution, strategy, risk, broker, startup or policy file changes; the frozen hashes hold.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

import freeze_ledger as FL  # noqa: E402

CSS = "frontend/product_ui.css"
SCOPED = "#pui-chart-empty .pui-chart-empty"


def _git(*a):
    return subprocess.run(["git", *a], cwd=ROOT, capture_output=True, text=True).stdout


def at_base(rel):
    run = subprocess.run(["git", "show", f"{FL.BASE_611FIX}:{rel}"], cwd=ROOT, capture_output=True)
    assert run.returncode == 0, f"{rel} is not in {FL.BASE_611FIX} (fetch-depth 0 is required)"   # an empty file (api/__init__.py) is a valid answer
    return run.stdout.decode("utf-8").replace("\r\n", "\n")


def post(rel):
    """PR #37's merged result, before the separately pinned ops changes."""
    run = subprocess.run(["git", "show", f"{FL.BASE_OPS}:{rel}"], cwd=ROOT, capture_output=True)
    assert run.returncode == 0, rel
    return run.stdout.decode("utf-8").replace("\r\n", "\n")


def now_text(rel):
    return (ROOT / rel).read_text(encoding="utf-8").replace("\r\n", "\n")


def test_C01_the_overlay_rules_are_scoped_and_no_bare_rule_remains():
    css = now_text(CSS)
    lines = [l for l in css.split("\n") if l.startswith(SCOPED)]
    assert [l.split("{")[0].strip() for l in lines] == [SCOPED, SCOPED + " h3", SCOPED + ".warn h3", SCOPED + " p"]
    assert not [l for l in css.split("\n") if l.startswith(".pui-chart-empty")]                 # the bare selector would also match <body class="pui pui-chart-empty">
    assert "position: absolute; inset: 0;" in lines[0] and "#pui-chart-empty:empty { display: none; }" in css
    for rule in ("body.pui.pui-chart-empty .tv-grid { height: auto; }", "body.pui.pui-chart-empty #tv-chart-note { display: none; }"):
        assert rule in css                                                                       # the body-state rules (chart strip, dimmed candles) are untouched


def test_C02_the_diff_to_main_is_exactly_the_four_selectors():
    css, base = now_text(CSS), at_base(CSS)
    note = "   /* the overlay only: body carries the same class name as a state flag */"
    assert css != base and css.count(SCOPED) == 4 and SCOPED not in base and css.count(note) == 1 and note not in base
    assert css.replace(SCOPED, ".pui-chart-empty").replace(note, "") == base                     # undoing the scoping (and its note) restores main byte for byte


def test_F01_the_fix_changes_exactly_the_declared_files_and_no_backend_file():
    assert FL.BASE_611FIX == "2fe29f3a48359a365a1506c9f70093129be72602" and subprocess.run(["git", "cat-file", "-e", f"{FL.BASE_611FIX}^{{commit}}"], cwd=ROOT).returncode == 0
    changed = set(_git("diff", "--name-only", FL.BASE_611FIX).split()) | set(_git("ls-files", "--others", "--exclude-standard").split())
    changed = {p for p in changed if not p.startswith(("scratchpad/", "runtime/", "data/", "logs/", "browser_tests/artifacts/")) and (ROOT / p).exists()}
    assert changed == FL.NEW_611FIX | FL.EDITED_611FIX | FL.NEW_OPS | FL.EDITED_OPS | FL.NEW_610D | FL.EDITED_610D | FL.NEW_542 | FL.EDITED_542
    changed = set(_git("diff", "--name-only", FL.BASE_611FIX, FL.BASE_OPS).split())
    assert changed == FL.NEW_611FIX | FL.EDITED_611FIX, {"unexpected": sorted(changed - FL.NEW_611FIX - FL.EDITED_611FIX), "missing": sorted((FL.NEW_611FIX | FL.EDITED_611FIX) - changed)}
    assert FL.SEMANTIC_611FIX == {} and {p for p in changed if not p.startswith((".github/", "tests/"))} == {CSS}   # one production file, CSS only
    in_base = set(_git("ls-tree", "-r", "--name-only", FL.BASE_611FIX).split())
    dirs = ("api/", "autorun/", "execution/", "safety/", "setups/", "ppe/", "proposals/", "evidence/", "decision_flow/", "dynamic_universe/", "unified_portfolio/", "trader_view/", "scripts/")
    for rel in sorted(p for p in in_base if p.startswith(dirs) and p.endswith((".py", ".ps1", ".json"))):
        assert post(rel) == at_base(rel), rel                                                # no execution, strategy, risk, broker, startup or policy file changed
    for rel in ("config.py", "frontend/product_ui.js", "frontend/index.html", "frontend/trader_view.js", "browser_tests/flows.py"):
        assert post(rel) == at_base(rel), rel


def test_F02_ready_thresholds_ppe_scoring_and_the_frozen_hashes_are_unchanged():
    out = subprocess.run([sys.executable, "-c", "import config, forward_validation as FV; from execution import exposure_gate as G; "
                          "print(FV.frozen_hash()); print(G.contract_hash(G.from_config(config))); print(G.contract_hash(G.GateSettings(enabled=True)))"],
                         cwd=ROOT, capture_output=True, text=True)
    assert out.stdout.split() == ["b5d4a56ef5bba61f8250b3cd25182cc1655738adf0605a77671fd029883f8530",
                                  "2552bd6639f691b828624250665519cc90c4a00da7f70893dbb635f7551006fa",
                                  "d2819342f6c8ccb1cd4a0ce2bfb5d1b0664f2453770c3e6736f7be420a17aed4"], out.stderr[-600:]
    import setups as S
    assert (S.WATCH_ENTER_STRETCH, S.READY_CONFIDENCE, S.READY_DEMAND, S.READY_DEMAND_COVERAGE, S.READY_PPE_COVERAGE, S.EVIDENCE_MAX_AGE_DAYS) == (-40.0, 40, 60, 0.60, 0.60, 30)


# every edit of a file no ledger entry can hold (an earlier stage's own test file): (text on main, text now), replayed by F03
REPLAY_611FIX = {'tests/test_bot_activity_68.py': [('    assert new == FL.NEW_68 | FL.NEW_69 | FL.NEW_691 | FL.NEW_610A | FL.NEW_610C | FL.NEW_541, {"unexpected_new": sorted(new - FL.NEW_68 - FL.NEW_69 - FL.NEW_691 '
                                    '- FL.NEW_610A - FL.NEW_610C - FL.NEW_541),\n'
                                    '                                                                     "missing_new": sorted((FL.NEW_68 | FL.NEW_69 | FL.NEW_691 | FL.NEW_610A | FL.NEW_610C | '
                                    'FL.NEW_541) - new)}   # + Stage 6.9 / 6.9.1 / 6.10A / 6.10C / 5.4.1\n',
                                    '    assert new == FL.NEW_68 | FL.NEW_69 | FL.NEW_691 | FL.NEW_610A | FL.NEW_610C | FL.NEW_541 | FL.NEW_611FIX, {"unexpected_new": sorted(new - FL.NEW_68 - '
                                    'FL.NEW_69 - FL.NEW_691 - FL.NEW_610A - FL.NEW_610C - FL.NEW_541 - FL.NEW_611FIX),\n'
                                    '                                                                     "missing_new": sorted((FL.NEW_68 | FL.NEW_69 | FL.NEW_691 | FL.NEW_610A | FL.NEW_610C | '
                                    'FL.NEW_541 | FL.NEW_611FIX) - new)}   # + Stage 6.9 / 6.9.1 / 6.10A / 6.10C / 5.4.1\n')],
 'tests/test_decision_flow_610c.py': [('    later = FL.NEW_541 | FL.EDITED_541                                                         # Stage 5.4.1, pinned by its own F tests\n',
                                       '    later = FL.NEW_541 | FL.EDITED_541 | FL.NEW_611FIX | FL.EDITED_611FIX                     # Stage 5.4.1 / the 6.1.1 CSS fix, each pinned by its own F '
                                       'tests\n'),
                                      ('        assert not [p for p in changed - later if p.startswith(d + "/")], d                  # ... and none since but Stage 5.4.1\'s declared files\n',
                                       '        assert not [p for p in changed - later if p.startswith(d + "/")], d                  # ... and none since but Stage 5.4.1\'s / the 6.1.1 CSS fix\'s '
                                       'declared files\n')],
 'tests/test_evidence_gate_541.py': [('\n\n# every Stage 5.4.1 edit of a file no ledger entry can hold',
                                      '\n'
                                      '\n'
                                      'def post(rel):\n'
                                      '    """main after PR #36 (Stage 5.4.1 merged): what Stage 5.4.1 left, whatever a later fix changed since."""\n'
                                      '    out = subprocess.run(["git", "show", f"{FL.BASE_611FIX}:{rel}"], cwd=ROOT, capture_output=True).stdout\n'
                                      '    assert out, f"{rel} is not in {FL.BASE_611FIX} (fetch-depth 0 is required)"\n'
                                      '    return out.decode("utf-8").replace("\\r\\n", "\\n")\n'
                                      '\n'
                                      '\n'
                                      '# every Stage 5.4.1 edit of a file no ledger entry can hold'),
                                     ('    changed = set(_git("diff", "--name-only", FL.BASE_541).split()) | set(_git("ls-files", "--others", "--exclude-standard").split())\n'
                                      '    changed = {p for p in changed if not p.startswith(("scratchpad/", "runtime/", "data/", "logs/", "browser_tests/artifacts/")) and (ROOT / p).exists()}\n'
                                      '    assert changed == FL.NEW_541 | FL.EDITED_541, {"unexpected": sorted(changed - FL.NEW_541 - FL.EDITED_541), "missing": sorted(FL.NEW_541 | FL.EDITED_541 - '
                                      'changed)}\n',
                                      '    stage541 = set(_git("diff", "--name-only", FL.BASE_541, FL.BASE_611FIX).split())        # Stage 5.4.1 itself, between two fixed commits (main before / '
                                      'after PR #36)\n'
                                      '    assert stage541 == FL.NEW_541 | FL.EDITED_541, {"unexpected": sorted(stage541 - FL.NEW_541 - FL.EDITED_541), "missing": sorted(FL.NEW_541 | FL.EDITED_541 - '
                                      'stage541)}\n'
                                      '    changed = set(_git("diff", "--name-only", FL.BASE_541).split()) | set(_git("ls-files", "--others", "--exclude-standard").split())\n'
                                      '    changed = {p for p in changed if not p.startswith(("scratchpad/", "runtime/", "data/", "logs/", "browser_tests/artifacts/")) and (ROOT / p).exists()}\n'
                                      '    later = FL.NEW_611FIX | FL.EDITED_611FIX                                                  # the Stage 6.1.1 CSS fix, pinned by its own F tests\n'
                                      '    assert changed == FL.NEW_541 | FL.EDITED_541 | later, {"unexpected": sorted(changed - FL.NEW_541 - FL.EDITED_541 - later), "missing": sorted((FL.NEW_541 | '
                                      'FL.EDITED_541 | later) - changed)}\n'),
                                     ('    for rel, edits in REPLAY_541.items():\n        text = (ROOT / rel).read_text(encoding="utf-8").replace("\\r\\n", "\\n")\n',
                                      '    for rel, edits in REPLAY_541.items():\n        text = post(rel)\n'),
                                     ('    fl, base = (ROOT / "tests/freeze_ledger.py").read_text(encoding="utf-8").replace("\\r\\n", "\\n"), at_base("tests/freeze_ledger.py")\n',
                                      '    fl, base = post("tests/freeze_ledger.py"), at_base("tests/freeze_ledger.py")\n'),
                                     ('    assert set(FL.LEDGER) - set(old) == {"trader_view/research.py"}                       # one new key: the corrected reader\n'
                                      '    assert {k for k in old if old[k] != FL.LEDGER[k]} == FL.EDITED_541 & set(old)          # every re-hash is a declared Stage 5.4.1 edit\n',
                                      '    mine = ast.literal_eval(led(fl)[len("LEDGER = "):])                                   # the ledger Stage 5.4.1 left (main after PR #36)\n'
                                      '    assert set(mine) - set(old) == {"trader_view/research.py"}                            # one new key: the corrected reader\n'
                                      '    assert {k for k in old if old[k] != mine[k]} == FL.EDITED_541 & set(old)               # every re-hash is a declared Stage 5.4.1 edit\n')],
 'tests/test_forward_paper_610a.py': [('    later = FL.NEW_610C | FL.EDITED_610C | FL.NEW_541 | FL.EDITED_541                         # Stage 6.10C / 5.4.1, each pinned by its own F tests\n',
                                       '    later = FL.NEW_610C | FL.EDITED_610C | FL.NEW_541 | FL.EDITED_541 | FL.NEW_611FIX | FL.EDITED_611FIX   # Stage 6.10C / 5.4.1 / the 6.1.1 CSS fix, each '
                                       'pinned by its own F tests\n'),
                                      ('        assert not [p for p in changed - later if p.startswith(d + "/")], d                  # ... and none since but Stage 6.10C\'s / 5.4.1\'s declared '
                                       'files\n',
                                       '        assert not [p for p in changed - later if p.startswith(d + "/")], d                  # ... and none since but Stage 6.10C\'s / 5.4.1\'s / the 6.1.1 '
                                       "CSS fix's declared files\n")],
 'tests/test_robinhood_portfolio_69.py': [('    later = FL.NEW_691 | FL.EDITED_691 | FL.NEW_610A | FL.EDITED_610A | FL.NEW_610C | FL.EDITED_610C | FL.NEW_541 | FL.EDITED_541   # Stage 6.9.1',
                                           '    later = FL.NEW_691 | FL.EDITED_691 | FL.NEW_610A | FL.EDITED_610A | FL.NEW_610C | FL.EDITED_610C | FL.NEW_541 | FL.EDITED_541 | FL.NEW_611FIX | '
                                           'FL.EDITED_611FIX   # the 6.1.1 CSS fix, Stage 6.9.1')],
 'tests/test_unified_exposure_gate_691.py': [('    later = FL.NEW_610A | FL.EDITED_610A | FL.NEW_610C | FL.EDITED_610C | FL.NEW_541 | FL.EDITED_541   # Stage 6.10A / 6.10C / 5.4.1, each pinned by '
                                              'its own F tests\n',
                                              '    later = FL.NEW_610A | FL.EDITED_610A | FL.NEW_610C | FL.EDITED_610C | FL.NEW_541 | FL.EDITED_541 | FL.NEW_611FIX | FL.EDITED_611FIX   # Stage 6.10A '
                                              '/ 6.10C / 5.4.1 / the 6.1.1 CSS fix, each pinned by its own F tests\n')]}


def test_F03_every_bookkeeping_edit_is_pinned_exactly_and_the_ledger_rehashes_only_the_declared_entries():
    import ast
    for rel, edits in REPLAY_611FIX.items():
        text = post(rel)
        for old, new in edits:
            assert text.count(new) == 1, (rel, new[:100])
            text = text.replace(new, old)
        assert text == at_base(rel), rel
    assert set(REPLAY_611FIX) == FL.EDITED_611FIX - set(FL.LEDGER) - {"tests/freeze_ledger.py"}   # every other edited file carries a ledger entry
    fl, base = post("tests/freeze_ledger.py"), at_base("tests/freeze_ledger.py")
    para = fl[fl.index("\nStage 6.1.1 CSS fix (chart empty state)"):fl.index('"""', fl.index("\nStage 6.1.1 CSS fix (chart empty state)"))]
    rebuilt = fl.replace(para, "", 1).replace(" + NEW_541 + NEW_611FIX plus this ledger.", " + NEW_541 plus this ledger.", 1)
    decl = rebuilt[rebuilt.index("BASE_611FIX = "):rebuilt.index("LEDGER = {")]
    rebuilt = rebuilt.replace(decl, "", 1)
    led = lambda t: t[t.index("LEDGER = {"):t.index("}\n", t.index("LEDGER = {")) + 2]  # noqa: E731
    rebuilt = rebuilt.replace(led(rebuilt), led(base), 1)
    assert rebuilt == base                                                                # the declarations, the paragraph, one line and the ledger - nothing else
    old = ast.literal_eval(led(base)[len("LEDGER = "):])
    mine = ast.literal_eval(led(fl)[len("LEDGER = "):])
    assert set(mine) == set(old)                                                     # no new key: every edited registered file was already registered
    assert {k for k in old if old[k] != mine[k]} == FL.EDITED_611FIX & set(old) == {CSS, ".github/ci/pytest_groups.json", "tests/test_strategy_validation_62.py"}
    for rel in FL.LEDGER:
        assert FL.view(rel, FL.now_text(rel)) == FL.base_text(rel), rel
