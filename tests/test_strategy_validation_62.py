"""
tests/test_strategy_validation_62.py — Stage 6.2 STRATEGY VALIDATION HARNESS (research / validation only; STRATEGY_VALIDATION_HARNESS.md,
DESIGN_62_STRATEGY_VALIDATION.md). Every fixture is SYNTHETIC and lives in tests/strategy_validation_fixtures.py; none is a registered strategy.

  S  candidates: Strategy Standard v1, the content identity, immutability, missing data skipped and counted (never zero), duplicates
  A  arms: one candidate set for RULES / GATED / MODEL, GATED veto + NO_ADDITIONAL_GATES, MODEL absent (NOT_EVALUATED) / filter-only / contract
  O  the chronological split, OOS reported on its own and independent of TRAIN, the sample-size rules
  M  simulation + metrics: causal fills, gross vs net, slippage, fees, t-stat, max drawdown, expectancy, profit factor, ambiguous bars, sizing
  D  diagnostics: TRAIN vs OOS, parameter sensitivity, period concentration, the TRAIN / OOS boundary
  U  the independent mechanical audit (duplicate, trade-before-signal, version mismatch, first-touch exit, segment, identity)
  L  look-ahead / causality checks
  V  the verdicts INSUFFICIENT_DATA / FAIL / MIXED / PASS / VALIDATION INVALID
  R  the Strategy Registry integration (validation fields, NOT_RUN for Downside Recovery, no lifecycle mutation), the store, the GET-only routes
  C  the CLI; the replay foundation; determinism
  B  the broker-write boundary (no execution / broker / Paper Auto import or call, not even with credentials in the environment)
  N  the Strategy Lab display (node tests + the real service payload rendered); CI manifest
  F  freeze: main 9fae08b + only the listed Stage 6.2 files and edits (the frozen controls, execution, risk and safety untouched)
  Z  documents
"""
import ast
import json
import math
import os
import re
import sqlite3
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
from freeze_ledger import NEW_68, NEW_69, NEW_691, NEW_610A, NEW_610C, NEW_541, NEW_610D, unchanged as ledger_unchanged, view as ledger_view  # noqa: E402 - Stage 6.8: files registered in the post-freeze ledger are read as on main (tests/freeze_ledger.py)
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

import strategy_validation_fixtures as F  # noqa: E402
from strategy_validation import (DEFAULT_FRICTION, DEFAULT_SIZING, DISCLAIMER, MIN_OOS_SESSIONS, MIN_OOS_TRADES, MIN_TRADES, T_STAT_WEAK, VERDICT_LABELS,  # noqa: E402
                                 VERSION)
from strategy_validation import arms as A  # noqa: E402
from strategy_validation import audit as AU  # noqa: E402
from strategy_validation import candidates as CD  # noqa: E402
from strategy_validation import causality as CA  # noqa: E402
from strategy_validation import diagnostics as DG  # noqa: E402
from strategy_validation import engine as E  # noqa: E402
from strategy_validation import metrics as MX  # noqa: E402
from strategy_validation import replay as RP  # noqa: E402
from strategy_validation import service as SV  # noqa: E402
from strategy_validation import simulator as SIM  # noqa: E402
from strategy_validation.dataset import Dataset, DatasetError, dump, load, split  # noqa: E402
from strategy_validation.models import RawCandidate  # noqa: E402

AT = "2026-10-25T00:00:00+00:00"
FROZEN_HASH = "b5d4a56ef5bba61f8250b3cd25182cc1655738adf0605a77671fd029883f8530"             # the Stage 6.1 frozen controls: unchanged

# ================================================================ the Stage 6.2 pins (the earlier freeze tests import them) ======================
BASE = "9fae08b8b55373bb161decc7e2b89ba1d27ecc6d"                                          # origin/main with the WAL bootstrap fix merged (PR #21)
NEW_62_OWN = {"DESIGN_62_STRATEGY_VALIDATION.md", "STRATEGY_VALIDATION_HARNESS.md", "api/routes/strategy_validation.py", "database/strategy_validation_migrations.py",
          "strategy_validation/__init__.py", "strategy_validation/__main__.py", "strategy_validation/adapters.py", "strategy_validation/arms.py",
          "strategy_validation/audit.py", "strategy_validation/candidates.py", "strategy_validation/causality.py", "strategy_validation/cli.py",
          "strategy_validation/dataset.py", "strategy_validation/diagnostics.py", "strategy_validation/engine.py", "strategy_validation/metrics.py",
          "strategy_validation/models.py", "strategy_validation/replay.py", "strategy_validation/service.py", "strategy_validation/simulator.py",
          "strategy_validation/store.py", "strategy_validation/verdict.py", "tests/frontend/strategy_validation.test.cjs", "tests/strategy_validation_fixtures.py",
          "tests/test_strategy_validation_62.py"}   # Stage 6.2 files
NEW_63 = {"DESIGN_63_DDR_REPLAY_READINESS.md"}                                                # Stage 6.3: the Downside Dislocation Recovery replay-readiness document
NEW_631 = {"strategy_validation/integrity.py", "tests/test_validation_integrity_631.py", "DESIGN_631_VALIDATION_HARDENING.md"}   # Stage 6.3.1: validation integrity
NEW_64 = {"value_quality/__init__.py", "value_quality/adapter.py", "value_quality/audit.py", "value_quality/core.py", "value_quality/data.py",
          "value_quality/diagnostics.py", "value_quality/ingest.py", "value_quality/pit.py", "strategy_registry/value_quality.py", "DESIGN_64_VALUE_QUALITY.md",
          "tests/test_value_quality_64.py", "tests/value_quality_fixtures.py", "tests/frontend/value_quality.test.cjs"}   # Stage 6.4: the Value + Quality strategy
NEW_641 = {"strategy_validation/cross_sectional.py", "DESIGN_641_CROSS_SECTIONAL_VALIDATION.md", "tests/test_cross_sectional_641.py",
           "tests/frontend/cross_sectional.test.cjs"}   # Stage 6.4.1: cross-sectional validation (validation_v3)
NEW_65 = {"momentum/__init__.py", "momentum/adapter.py", "momentum/audit.py", "momentum/core.py", "momentum/data.py", "momentum/diagnostics.py",
          "strategy_registry/momentum.py", "DESIGN_65_MOMENTUM.md", "tests/test_momentum_65.py", "tests/momentum_fixtures.py"}   # Stage 6.5: the Momentum strategy
NEW_651 = {"strategy_validation/universe.py", "DESIGN_651_UNIVERSE_INTEGRITY.md", "tests/test_universe_integrity_651.py",
           "tests/frontend/universe_integrity.test.cjs"}                                          # Stage 6.5.1: the universe integrity gate
NEW_66 = {"mean_reversion/__init__.py", "mean_reversion/adapter.py", "mean_reversion/audit.py", "mean_reversion/core.py", "mean_reversion/data.py",
          "mean_reversion/diagnostics.py", "strategy_registry/mean_reversion.py", "DESIGN_66_MEAN_REVERSION.md", "tests/test_mean_reversion_66.py",
          "tests/mean_reversion_fixtures.py", "tests/frontend/mean_reversion.test.cjs"}   # Stage 6.6: the Mean Reversion strategy
NEW_67 = {"breakout/__init__.py", "breakout/adapter.py", "breakout/audit.py", "breakout/core.py", "breakout/data.py", "breakout/diagnostics.py",
          "strategy_registry/breakout.py", "DESIGN_67_BREAKOUT.md", "tests/test_breakout_67.py", "tests/breakout_fixtures.py",
          "tests/frontend/breakout.test.cjs"}                                                     # Stage 6.7: the Breakout strategy
NEW_62 = NEW_62_OWN | NEW_63 | NEW_631 | NEW_64 | NEW_641 | NEW_65 | NEW_651 | NEW_66 | NEW_67 | NEW_68 | NEW_69 | NEW_691 | NEW_610A | NEW_610C | NEW_541 | NEW_610D   # Stage 6.8 / 6.9 / 6.9.1 / 6.10A / 6.10C / 5.4.1 / 6.10D: + their new files (tests/freeze_ledger.py); every file added after main 9fae08b: the earlier freeze tests import THIS set, so a later stage extends it here, nowhere else
CHANGED_62 = {".github/ci/pytest_groups.json", "README.md", "api/routes/strategy_registry.py", "browser_tests/flows.py", "frontend/product_ui.css", "frontend/product_ui.js",
              "strategy_registry/service.py", "tests/test_forward_validation_61.py", "tests/test_product_ui_611.py", "tests/test_product_flow_612.py", "tests/test_db_bootstrap_busy.py",
              "strategy_registry/registry.py"}   # + Stage 6.4 (VALUE_QUALITY implemented)
MARK_62 = "# >>> " + "Strategy Validation 6.2"                                                  # built, not literal: only browser_tests/flows.py carries it
STRIP_62 = {                                                                                   # the marked / delimited Stage 6.2 blocks, removed before the exact edits are undone
    "browser_tests/flows.py": r"(?ms)^# >>> Strategy Validation 6\.2\b.*?^# <<< Strategy Validation 6\.2[^\n]*\n\n\n",
    "README.md": r"(?ms)^## Strategy validation harness \(Stage 6\.2, research only\)\n.*?(?=^## Continuous integration \(Stage 6\.0\.3)",
    "frontend/product_ui.js": r"(?ms)^  // Stage 6\.2: the historical validation of a strategy .*?\n\n(?=  // one strategy's detail:)",
    "frontend/product_ui.css": r"(?ms)^/\* Stage 6\.2: the Validation block .*?(?=\n/\* -+ responsive)",
}
EDITS_62_SR = [                                                                                    # strategy_registry/service.py: the validation fields of a row and the validation block of the detail
    ('\ndef registry_view(facts: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:\n    rows = [_row(e, facts) for e in R.entries()]\n    sources = [{"source": s, "label": SOURCE_LABELS[s], "count": sum(1 for r in rows if r["source"] == s),\n',
     '\ndef _validation_summary(entry: StrategyEntry, validations: Optional[Dict[str, dict]]) -> dict:\n    """Stage 6.2: the validation fields of a registry row (the latest stored run, or NOT_RUN and why). Display only: never a lifecycle input."""\n    from strategy_validation import service as SV\n    return (validations or {}).get(entry.strategy_id) or SV.summary(None, entry.strategy_id, entry)\n\n\ndef registry_view(facts: Optional[Dict[str, Any]] = None, validations: Optional[Dict[str, dict]] = None) -> Dict[str, Any]:\n    rows = [{**_row(e, facts), **_validation_summary(e, validations)} for e in R.entries()]\n    sources = [{"source": s, "label": SOURCE_LABELS[s], "count": sum(1 for r in rows if r["source"] == s),\n'),
    ('\ndef detail_view(strategy_id: str, facts: Optional[Dict[str, Any]] = None) -> Optional[Dict[str, Any]]:\n    entry = R.get(strategy_id)\n',
     '\ndef detail_view(strategy_id: str, facts: Optional[Dict[str, Any]] = None, validation: Optional[dict] = None) -> Optional[Dict[str, Any]]:\n    entry = R.get(strategy_id)\n'),
    ('    d["version"], d["standard_version"] = VERSION, STANDARD_VERSION\n    return d\n',
     '    d["version"], d["standard_version"] = VERSION, STANDARD_VERSION\n    if validation is None:                                                     # Stage 6.2: the Validation section (latest stored run, or "Not yet evaluated")\n        from strategy_validation import service as SV\n        validation = SV.detail(None, entry.strategy_id, entry)\n    d["validation"] = validation\n    d["entry"].update({k: validation.get(k) for k in ("validation_status", "validation_label", "not_run_reason", "latest_validation_run_id", "validation_verdict",\n                                                      "train_trade_count", "oos_trade_count", "mechanical_audit_status", "last_validated_at", "validation_synthetic")})\n    return d\n'),
]
EDITS_62_ROUTE = [                                                                                 # api/routes/strategy_registry.py: the validation store read + the GET-only validation sub-router
    ('\n\n@router.get("")\ndef get_registry() -> JSONResponse:\n    return JSONResponse(SR.registry_view(_facts()))\n\n\n',
     '\n\ndef _validation_store():\n    """Stage 6.2: the stored validation runs (read only); None when the database cannot be opened (the rows then read NOT_RUN)."""\n    try:\n        from strategy_validation.store import StrategyValidationStore\n        return StrategyValidationStore()\n    except Exception:  # noqa: BLE001 - display only\n        return None\n\n\n@router.get("")\ndef get_registry() -> JSONResponse:\n    from strategy_registry import registry as R\n    from strategy_validation import service as SV\n    store = _validation_store()\n    return JSONResponse(SR.registry_view(_facts(), SV.registry_summaries(store, R.entries()) if store is not None else None))\n\n\n'),
    ('    if not _ID.match(strategy_id or ""):\n        return JSONResponse({"status": "INVALID", "message": "strategy_id must be UPPER_SNAKE_CASE"}, status_code=400)\n    d = SR.detail_view(strategy_id.upper(), _facts())\n    if d is None:\n        return JSONResponse({"status": "NOT_FOUND", "message": f"no strategy {strategy_id.upper()} in the registry"}, status_code=404)\n    return JSONResponse(d)\n',
     '    if not _ID.match(strategy_id or ""):\n        return JSONResponse({"status": "INVALID", "message": "strategy_id must be UPPER_SNAKE_CASE"}, status_code=400)\n    from strategy_registry import registry as R\n    from strategy_validation import service as SV\n    entry, store = R.get(strategy_id), _validation_store()\n    validation = SV.detail(store.latest(entry.strategy_id) if (entry and store) else None, strategy_id.upper(), entry) if entry else None\n    d = SR.detail_view(strategy_id.upper(), _facts(), validation)\n    if d is None:\n        return JSONResponse({"status": "NOT_FOUND", "message": f"no strategy {strategy_id.upper()} in the registry"}, status_code=404)\n    return JSONResponse(d)\n\n\n# Stage 6.2: the validation views (GET only) live under this prefix: /{strategy_id}/validation and /validation/runs/{run_id}[/simulations | /audit]\nfrom api.routes import strategy_validation as _validation_routes  # noqa: E402\n\nrouter.include_router(_validation_routes.router)\n'),
]
EDITS_62_JS = [                                                                                    # frontend/product_ui.js outside its Stage 6.2 block: the column, the cell, the block in the detail, two exports
    ('      `<table class="pui-registry" data-pui-registry><thead><tr><th>Strategy</th><th>Source</th><th>Lifecycle</th><th>Horizon</th><th>Direction</th><th>Version</th><th>Updated</th></tr></thead><tbody>` +\n',
     '      `<table class="pui-registry" data-pui-registry><thead><tr><th>Strategy</th><th>Source</th><th>Lifecycle</th><th>Validation</th><th>Horizon</th><th>Direction</th><th>Version</th><th>Updated</th></tr></thead><tbody>` +\n'),
    ('        `<td>${esc(s.source)}</td><td>${badge(s)}</td><td>${esc(words(s.time_horizon))}</td><td>${esc(words(s.direction_capability))}</td><td>${esc(s.version)}</td><td>${esc(s.updated_at)}</td></tr>`).join("") + `</tbody></table>` +\n',
     '        `<td>${esc(s.source)}</td><td>${badge(s)}</td><td>${svBadge(s)}</td><td>${esc(words(s.time_horizon))}</td><td>${esc(words(s.direction_capability))}</td><td>${esc(s.version)}</td><td>${esc(s.updated_at)}</td></tr>`).join("") + `</tbody></table>` +\n'),
    ('      (det.sections || []).map((s, i) => sec(s, i === 0)).join("") +\n',
     '      validationHtml(det.validation) + (det.sections || []).map((s, i) => sec(s, i === 0)).join("") +\n'),
    ('           dedupe, activityItems, filterItems, dailyReview, focusHtml, whyHtml, closedHtml, registryHtml, strategyDetailHtml, openRoute, openMore, renderLab };\n',
     '           dedupe, activityItems, filterItems, dailyReview, focusHtml, whyHtml, closedHtml, registryHtml, strategyDetailHtml, openRoute, openMore, renderLab,\n           // Stage 6.2\n           svBadge, validationHtml };\n'),
]
EDITS_62_FLOWS = [                                                                                 # browser_tests/flows.py outside its marked block: the two flows after scanner
    ('         ai_evidence, history, scanner, small_screens, trader_view, market_demand, setup_state, trade_review, risk_controls, paper_bot, account_refresh, cash_flow, forward_validation, product_ui, product_flow, product_flow_degraded, product_flow_lab, product_flow_links, product_flow_ready, product_flow_session, automation, saved_scans, daily_brief, brief_delivery, notification_click,\n',
     '         ai_evidence, history, scanner, strategy_validation, strategy_validation_results, small_screens, trader_view, market_demand, setup_state, trade_review, risk_controls, paper_bot, account_refresh, cash_flow, forward_validation, product_ui, product_flow, product_flow_degraded, product_flow_lab, product_flow_links, product_flow_ready, product_flow_session, automation, saved_scans, daily_brief, brief_delivery, notification_click,\n'),
]
GROUPS_EDITS_631 = [                                                                               # Stage 6.3.1: its test file in the research group (undone first)
    ('        "test_strategy_validation_62.py"\n      ]', '        "test_strategy_validation_62.py",\n        "test_validation_integrity_631.py"\n      ]'),
]
GROUPS_EDITS_62 = [                                                                                # .github/ci/pytest_groups.json: this file in the research group
    ('      "about": "signal research, rotation diagnostics / engine / handoff / store / API, research workflow, portfolio backtest",\n',
     '      "about": "signal research, rotation diagnostics / engine / handoff / store / API, research workflow, portfolio backtest, strategy validation harness",\n'),
    ('        "test_signal_research_52.py"\n      ]',
     '        "test_signal_research_52.py",\n        "test_strategy_validation_62.py"\n      ]'),
]
EDITS_62_61 = [                                                                                    # tests/test_forward_validation_61.py
    ('NEW_FIX = {"tests/test_db_bootstrap_busy.py"}   # the WAL bootstrap fix (tests/test_db_bootstrap_busy.py pins database/database.py)\n',
     'NEW_FIX = {"tests/test_db_bootstrap_busy.py"}   # the WAL bootstrap fix (tests/test_db_bootstrap_busy.py pins database/database.py)\nfrom test_strategy_validation_62 import NEW_62  # noqa: E402 - Stage 6.2: the strategy validation harness files (tests/test_strategy_validation_62.py pins them)\n'),
    ('    assert new == NEW_FILES | NEW_611 | NEW_612 | NEW_FIX, {"unexpected_new": sorted(new - NEW_FILES - NEW_611 - NEW_612 - NEW_FIX), "missing_new": sorted((NEW_FILES | NEW_611 | NEW_612 | NEW_FIX) - new)}   # Stage 6.1.1 / 6.1.2 files + the WAL bootstrap fix\n',
     '    assert new == NEW_FILES | NEW_611 | NEW_612 | NEW_FIX | NEW_62, {"unexpected_new": sorted(new - NEW_FILES - NEW_611 - NEW_612 - NEW_FIX - NEW_62), "missing_new": sorted((NEW_FILES | NEW_611 | NEW_612 | NEW_FIX | NEW_62) - new)}   # Stage 6.1.1 / 6.1.2 files + the WAL bootstrap fix + Stage 6.2\n'),
]
EDITS_62_611 = [                                                                                   # tests/test_product_ui_611.py
    ('from test_db_bootstrap_busy import EDITS_61_FIX, NEW_FIX  # noqa: E402 - the WAL bootstrap fix: its new test file and its in-place edits of the Stage 6.1 test\n',
     'from test_db_bootstrap_busy import EDITS_61_FIX, NEW_FIX  # noqa: E402 - the WAL bootstrap fix: its new test file and its in-place edits of the Stage 6.1 test\nfrom test_strategy_validation_62 import EDITS_62_61, NEW_62  # noqa: E402 - Stage 6.2: its new files and its in-place edits of the Stage 6.1 test\n'),
    ('    assert new == NEW_611 | NEW_612 | NEW_FIX, {"unexpected_new": sorted(new - NEW_611 - NEW_612 - NEW_FIX), "missing_new": sorted((NEW_611 | NEW_612 | NEW_FIX) - new)}   # Stage 6.1.2 files + the WAL bootstrap fix\n',
     '    assert new == NEW_611 | NEW_612 | NEW_FIX | NEW_62, {"unexpected_new": sorted(new - NEW_611 - NEW_612 - NEW_FIX - NEW_62), "missing_new": sorted((NEW_611 | NEW_612 | NEW_FIX | NEW_62) - new)}   # Stage 6.1.2 files + the WAL bootstrap fix + Stage 6.2\n'),
    ("    for old, new in EDITS_61_FIX:                                                                 # the WAL bootstrap fix: its in-place edits, applied after Stage 6.1.2's\n        assert text.count(old) == 1 and new not in text, old[:70]\n        text = text.replace(old, new)\n",
     "    for old, new in EDITS_61_FIX:                                                                 # the WAL bootstrap fix: its in-place edits, applied after Stage 6.1.2's\n        assert text.count(old) == 1 and new not in text, old[:70]\n        text = text.replace(old, new)\n    for old, new in EDITS_62_61:                                                                  # Stage 6.2: its in-place edits, applied after the fix's\n        assert text.count(old) == 1 and new not in text, old[:70]\n        text = text.replace(old, new)\n"),
]
EDITS_62_612_OWN = [                                                                               # tests/test_product_flow_612.py
    ('from test_db_bootstrap_busy import EDITS_61_FIX, EDITS_611_FIX, GROUPS_EDIT_FIX, NEW_FIX  # noqa: E402 - the WAL bootstrap fix pins (tests/test_db_bootstrap_busy.py)\n',
     'from test_db_bootstrap_busy import EDITS_61_FIX, EDITS_611_FIX, GROUPS_EDIT_FIX, NEW_FIX  # noqa: E402 - the WAL bootstrap fix pins (tests/test_db_bootstrap_busy.py)\nfrom test_strategy_validation_62 import EDITS_62_61, EDITS_62_611, NEW_62, undo_62  # noqa: E402 - the Stage 6.2 pins (tests/test_strategy_validation_62.py)\n'),
    ('    assert mine == {"/api/strategy-registry": {"get"}, "/api/strategy-registry/standard": {"get"}, "/api/strategy-registry/{strategy_id}": {"get"}}\n',
     '    assert mine == {"/api/strategy-registry": {"get"}, "/api/strategy-registry/standard": {"get"}, "/api/strategy-registry/{strategy_id}": {"get"},\n                    "/api/strategy-registry/{strategy_id}/validation": {"get"}, "/api/strategy-registry/validation/runs/{run_id}": {"get"},   # Stage 6.2: the validation views (GET only)\n                    "/api/strategy-registry/validation/runs/{run_id}/simulations": {"get"}, "/api/strategy-registry/validation/runs/{run_id}/audit": {"get"}}\n'),
    ('    assert new == NEW_612 | NEW_FIX, {"unexpected_new": sorted(new - NEW_612 - NEW_FIX), "missing_new": sorted((NEW_612 | NEW_FIX) - new)}   # + the WAL bootstrap fix test\n',
     '    assert new == NEW_612 | NEW_FIX | NEW_62, {"unexpected_new": sorted(new - NEW_612 - NEW_FIX - NEW_62), "missing_new": sorted((NEW_612 | NEW_FIX | NEW_62) - new)}   # + the WAL bootstrap fix test + Stage 6.2\n'),
    ('    assert replay(blob("tests/test_product_ui_611.py"), EDITS_611 + EDITS_611_FIX) == now_text("tests/test_product_ui_611.py"), "tests/test_product_ui_611.py: other than the listed in-place edits"\n',
     '    assert replay(blob("tests/test_product_ui_611.py"), EDITS_611 + EDITS_611_FIX + EDITS_62_611) == now_text("tests/test_product_ui_611.py"), "tests/test_product_ui_611.py: other than the listed in-place edits"\n'),
    ('    assert replay(blob("tests/test_forward_validation_61.py"), EDITS_612_61 + EDITS_61_FIX) == now_text("tests/test_forward_validation_61.py"), "tests/test_forward_validation_61.py: other than the listed edits"\n',
     '    assert replay(blob("tests/test_forward_validation_61.py"), EDITS_612_61 + EDITS_61_FIX + EDITS_62_61) == now_text("tests/test_forward_validation_61.py"), "tests/test_forward_validation_61.py: other than the listed edits"\n'),
    ('    flows = now_text("browser_tests/flows.py")\n',
     '    flows = undo_62("browser_tests/flows.py", now_text("browser_tests/flows.py"))                 # Stage 6.2: its marked block and its FLOWS edit undone first\n'),
    ('    assert blob(".github/ci/pytest_groups.json").count(old) == 1 and blob(".github/ci/pytest_groups.json").replace(old, new).replace(*GROUPS_EDIT_FIX) == now_text(".github/ci/pytest_groups.json")\n',
     '    assert blob(".github/ci/pytest_groups.json").count(old) == 1 and blob(".github/ci/pytest_groups.json").replace(old, new).replace(*GROUPS_EDIT_FIX) == undo_62(".github/ci/pytest_groups.json", now_text(".github/ci/pytest_groups.json"))\n'),
    ('    readme = now_text("README.md")\n    section = re.findall(',
     '    readme = undo_62("README.md", now_text("README.md"))                                          # Stage 6.2: its README section removed first\n    section = re.findall('),
]
EDITS_62_FIX = [                                                                                   # tests/test_db_bootstrap_busy.py
    ('import database.database as DB  # noqa: E402\n',
     'import database.database as DB  # noqa: E402\nfrom test_strategy_validation_62 import CHANGED_62, EDITS_62_61, EDITS_62_611, EDITS_62_612, NEW_62, undo_62  # noqa: E402 - the Stage 6.2 pins (tests/test_strategy_validation_62.py)\n'),
    ('    assert new == NEW_FIX, {"unexpected_new": sorted(new - NEW_FIX), "missing_new": sorted(NEW_FIX - new)}\n    assert changed == CHANGED_FIX, {"unexpected_changes": sorted(changed - CHANGED_FIX), "missing_changes": sorted(CHANGED_FIX - changed)}\n',
     '    assert new == NEW_FIX | NEW_62, {"unexpected_new": sorted(new - NEW_FIX - NEW_62), "missing_new": sorted((NEW_FIX | NEW_62) - new)}   # + Stage 6.2\n    assert changed == CHANGED_FIX | (CHANGED_62 - NEW_FIX), {"unexpected_changes": sorted(changed - CHANGED_FIX - CHANGED_62), "missing_changes": sorted((CHANGED_FIX | (CHANGED_62 - NEW_FIX)) - changed)}\n'),
    ('    assert replay(blob("tests/test_forward_validation_61.py"), EDITS_61_FIX) == now_text("tests/test_forward_validation_61.py")\n    assert replay(blob("tests/test_product_ui_611.py"), EDITS_611_FIX) == now_text("tests/test_product_ui_611.py")\n    assert replay(blob("tests/test_product_flow_612.py"), EDITS_612_FIX) == now_text("tests/test_product_flow_612.py")\n',
     '    assert replay(blob("tests/test_forward_validation_61.py"), EDITS_61_FIX + EDITS_62_61) == now_text("tests/test_forward_validation_61.py")   # + Stage 6.2\'s edits\n    assert replay(blob("tests/test_product_ui_611.py"), EDITS_611_FIX + EDITS_62_611) == now_text("tests/test_product_ui_611.py")\n    assert replay(blob("tests/test_product_flow_612.py"), EDITS_612_FIX + EDITS_62_612) == now_text("tests/test_product_flow_612.py")\n'),
    ('    assert groups.count(GROUPS_EDIT_FIX[0]) == 1 and groups.replace(*GROUPS_EDIT_FIX) == now_text(".github/ci/pytest_groups.json")\n',
     '    assert groups.count(GROUPS_EDIT_FIX[0]) == 1 and groups.replace(*GROUPS_EDIT_FIX) == undo_62(".github/ci/pytest_groups.json", now_text(".github/ci/pytest_groups.json"))   # Stage 6.2 undone first\n'),
]
# ---------------------------------------------------------------- Stage 6.4 (Value + Quality): its files and its exact in-place edits of earlier files
EDITS_64_REG = [                                                                                   # strategy_registry/registry.py: VALUE_QUALITY implemented (research), no longer a placeholder
    ('PLACEHOLDER_IDS = ("VALUE_QUALITY", "MOMENTUM", "MEAN_REVERSION", "BREAKOUT")\n',
     'PLACEHOLDER_IDS = ("MOMENTUM", "MEAN_REVERSION", "BREAKOUT")       # Stage 6.4: VALUE_QUALITY is implemented (research only)\n'),
    ('                  status="RESEARCH", version="0.0", description="Future internal strategy (value and quality factors). " + PLACEHOLDER_NOTE,\n                  created_at=REGISTRY_DATE, updated_at=REGISTRY_DATE, implemented=False, placeholder_note=PLACEHOLDER_NOTE),\n',
     '                  status="RESEARCH", version="1.0",\n                  description=("Long-only monthly rebalance of the 20 highest-ranked stocks by an equal-weight composite of value (earnings, book and operating "\n                               "cash-flow yield) and quality (return on assets, cash flow on assets, equity ratio), from point-in-time SEC fundamentals "\n                               "(Stage 6.4, value_quality/). Research only: it is validated historically and never produces a trading signal."),\n                  created_at=REGISTRY_DATE, updated_at="2026-10-25", implemented=True),\n'),
]
EDITS_64_SR = [                                                                                    # strategy_registry/service.py: the Value + Quality detail sections
    ('        sections, links = DR.sections(facts), DR.LINKS\n    else:\n',
     '        sections, links = DR.sections(facts), DR.LINKS\n    elif entry.implemented and entry.strategy_id == "VALUE_QUALITY":                # Stage 6.4: research strategy #2 (documented from its frozen rules)\n        from strategy_registry import value_quality as VQS\n        sections, links = VQS.sections(), VQS.LINKS\n    else:\n'),
]
EDITS_64_FLOWS = [                                                                                 # browser_tests/flows.py (outside the 6.2 block): the Strategy Lab registry check
    ('PF_PLACEHOLDERS = ["VALUE_QUALITY", "MOMENTUM", "MEAN_REVERSION", "BREAKOUT"]\n',
     'PF_PLACEHOLDERS = ["MOMENTUM", "MEAN_REVERSION", "BREAKOUT"]                                 # Stage 6.4: VALUE_QUALITY is implemented (research only)\n'),
    ('    four placeholders marked placeholder · not active, QuantConnect as a supported source with nothing registered; the detail view shows the thirteen sections with\n',
     '    three placeholders marked placeholder · not active (Stage 6.4: Value + Quality is implemented, research, not active), QuantConnect as a supported source with\n    nothing registered; the detail view shows the thirteen sections with\n'),
    ('    c.r.check("registry: the five entries in the API\'s order; Downside Dislocation Recovery implemented + active with its derived lifecycle label; the four placeholders not implemented, not active, marked",\n',
     '    c.r.check("registry: the five entries in the API\'s order; Downside Dislocation Recovery implemented + active with its derived lifecycle label; the three placeholders not implemented, not active, marked; "\n              "Value + Quality implemented, Research, not active, no placeholder mark",\n'),
    ('              and all(r[1] == "0" and r[2] == "0" and "placeholder · not active" in r[3] for r in rows if r[0] in PF_PLACEHOLDERS) and len([r for r in rows if r[0] in PF_PLACEHOLDERS]) == 4\n',
     '              and all(r[1] == "0" and r[2] == "0" and "placeholder · not active" in r[3] for r in rows if r[0] in PF_PLACEHOLDERS) and len([r for r in rows if r[0] in PF_PLACEHOLDERS]) == 3\n              and any(r[0] == "VALUE_QUALITY" and r[1:3] == ["1", "0"] and "Research" in r[3] and "placeholder" not in r[3] for r in rows)\n'),
]
GROUPS_EDITS_64 = [                                                                                # .github/ci/pytest_groups.json: tests/test_value_quality_64.py in the research group (undone first)
    ('        "test_strategy_validation_62.py",\n        "test_validation_integrity_631.py"\n      ]',
     '        "test_strategy_validation_62.py",\n        "test_validation_integrity_631.py",\n        "test_value_quality_64.py"\n      ]'),
]
EDITS_64_612 = [                                                                                   # tests/test_product_flow_612.py: VALUE_QUALITY implemented (research), three placeholders
    ('    for pid in ("VALUE_QUALITY", "MOMENTUM", "MEAN_REVERSION", "BREAKOUT"):\n        assert rows[pid]["implemented"] is False and rows[pid]["active"] is False and rows[pid]["status"] == "RESEARCH" and rows[pid]["setup_type"] is None and "not implemented" in rows[pid]["placeholder_note"]\n',
     '    vq = rows["VALUE_QUALITY"]                                                                    # Stage 6.4: implemented as a RESEARCH strategy - never active, no setup type\n    assert vq["implemented"] is True and vq["active"] is False and vq["status"] == vq["declared_status"] == "RESEARCH" and vq["setup_type"] is None and vq["placeholder_note"] is None\n    for pid in ("MOMENTUM", "MEAN_REVERSION", "BREAKOUT"):\n        assert rows[pid]["implemented"] is False and rows[pid]["active"] is False and rows[pid]["status"] == "RESEARCH" and rows[pid]["setup_type"] is None and "not implemented" in rows[pid]["placeholder_note"]\n'),
    ('"PAPER_ACTIVE", "APPROVED", "REJECTED"] and j["counts"] == {"strategies": 5, "implemented": 1, "placeholders": 4}\n',
     '"PAPER_ACTIVE", "APPROVED", "REJECTED"] and j["counts"] == {"strategies": 5, "implemented": 2, "placeholders": 3}   # Stage 6.4: VALUE_QUALITY implemented\n'),
    ('"BREAKOUT"] and [e.strategy_id for e in R.implemented()] == ["DOWNSIDE_DISLOCATION_RECOVERY"]\n',
     '"BREAKOUT"] and [e.strategy_id for e in R.implemented()] == ["DOWNSIDE_DISLOCATION_RECOVERY", "VALUE_QUALITY"]   # + Stage 6.4\n'),
    ('        assert (e.implemented and e.status == "PAPER_CANDIDATE" and e.setup_type == "DOWNSIDE_DISLOCATION_RECOVERY") or (not e.implemented and e.status == "RESEARCH" and e.setup_type is None and e.placeholder_note)\n',
     '        assert (e.implemented and e.status == "PAPER_CANDIDATE" and e.setup_type == "DOWNSIDE_DISLOCATION_RECOVERY") or (not e.implemented and e.status == "RESEARCH" and e.setup_type is None and e.placeholder_note) \\\n            or (e.implemented and e.strategy_id == "VALUE_QUALITY" and e.status == "RESEARCH" and e.setup_type is None and not e.placeholder_note)   # Stage 6.4: a research strategy\n'),
    ('    assert v["counts"] == {"strategies": 5, "implemented": 1, "placeholders": 4} and [s["active"] for s in v["strategies"]] == [True, False, False, False, False]\n',
     '    assert v["counts"] == {"strategies": 5, "implemented": 2, "placeholders": 3} and [s["active"] for s in v["strategies"]] == [True, False, False, False, False]   # Stage 6.4\n'),
    ('    for pid in ("VALUE_QUALITY", "MOMENTUM", "MEAN_REVERSION", "BREAKOUT"):\n        with pytest.raises(StrategyStandardError) as e:\n            validate_signal(signal(strategy_id=pid), reg)\n        assert e.value.code == "STRATEGY_NOT_IMPLEMENTED"                                        # a future placeholder never appears as an active signal\n',
     '    for pid in ("MOMENTUM", "MEAN_REVERSION", "BREAKOUT"):\n        with pytest.raises(StrategyStandardError) as e:\n            validate_signal(signal(strategy_id=pid), reg)\n        assert e.value.code == "STRATEGY_NOT_IMPLEMENTED"                                        # a future placeholder never appears as an active signal\n    with pytest.raises(StrategyStandardError) as e:                                              # Stage 6.4: a RESEARCH strategy never emits a live signal either\n        validate_signal(signal(strategy_id="VALUE_QUALITY"), reg)\n    assert e.value.code == "STRATEGY_INACTIVE"\n'),
]
GROUPS_EDITS_641 = [                                                                               # .github/ci/pytest_groups.json: tests/test_cross_sectional_641.py (undone first)
    ('        "test_value_quality_64.py"\n      ]',
     '        "test_value_quality_64.py",\n        "test_cross_sectional_641.py"\n      ]'),
]
# ---------------------------------------------------------------- Stage 6.5 (Momentum): its files and its exact in-place edits of earlier files
EDITS_65_REG = [                                                                                   # strategy_registry/registry.py: MOMENTUM implemented (research), no longer a placeholder
    ('PLACEHOLDER_IDS = ("MOMENTUM", "MEAN_REVERSION", "BREAKOUT")       # Stage 6.4: VALUE_QUALITY is implemented (research only)\n',
     'PLACEHOLDER_IDS = ("MEAN_REVERSION", "BREAKOUT")                  # Stage 6.4: VALUE_QUALITY, Stage 6.5: MOMENTUM are implemented (research only)\n'),
    ('    StrategyEntry(strategy_id="MOMENTUM", display_name="Momentum", source="INTERNAL", asset_class="US_EQUITY", direction_capability="LONG", time_horizon="SWING",\n                  status="RESEARCH", version="0.0", description="Future internal strategy (price momentum). " + PLACEHOLDER_NOTE,\n                  created_at=REGISTRY_DATE, updated_at=REGISTRY_DATE, implemented=False, placeholder_note=PLACEHOLDER_NOTE),\n',
     '    StrategyEntry(strategy_id="MOMENTUM", display_name="Momentum", source="INTERNAL", asset_class="US_EQUITY", direction_capability="LONG", time_horizon="POSITION",\n                  status="RESEARCH", version="1.0",\n                  description=("Long-only monthly rebalance of the 20 stocks with the strongest 12-1 price momentum (the adjusted return from about 12 months "\n                               "to about 1 month before the rebalance), equal weight, from daily bars (Stage 6.5, momentum/). Research only: it is "\n                               "validated historically against its eligible universe and never produces a trading signal."),\n                  created_at=REGISTRY_DATE, updated_at="2026-10-26", implemented=True),\n'),
]
EDITS_65_SR = [                                                                                    # strategy_registry/service.py: the Momentum detail sections
    ('        sections, links = VQS.sections(), VQS.LINKS\n    else:\n',
     '        sections, links = VQS.sections(), VQS.LINKS\n    elif entry.implemented and entry.strategy_id == "MOMENTUM":                     # Stage 6.5: research strategy #3 (documented from its frozen rules)\n        from strategy_registry import momentum as MOS\n        sections, links = MOS.sections(), MOS.LINKS\n    else:\n'),
]
EDITS_65_FLOWS = [                                                                                 # browser_tests/flows.py (outside the 6.2 block): the Strategy Lab registry check
    ('PF_PLACEHOLDERS = ["MOMENTUM", "MEAN_REVERSION", "BREAKOUT"]                                 # Stage 6.4: VALUE_QUALITY is implemented (research only)\n',
     'PF_PLACEHOLDERS = ["MEAN_REVERSION", "BREAKOUT"]                                             # Stage 6.4 / 6.5: VALUE_QUALITY and MOMENTUM are implemented (research only)\n'),
    ('    three placeholders marked placeholder · not active (Stage 6.4: Value + Quality is implemented, research, not active), QuantConnect as a supported source with\n',
     '    two placeholders marked placeholder · not active (Stage 6.4 / 6.5: Value + Quality and Momentum are implemented, research, not active), QuantConnect as a\n    supported source with\n'),
    ('the three placeholders not implemented, not active, marked; "\n              "Value + Quality implemented, Research, not active, no placeholder mark",\n',
     'the two placeholders not implemented, not active, marked; "\n              "Value + Quality and Momentum implemented, Research, not active, no placeholder mark",\n'),
    ('if r[0] in PF_PLACEHOLDERS]) == 3\n              and any(r[0] == "VALUE_QUALITY" and r[1:3] == ["1", "0"] and "Research" in r[3] and "placeholder" not in r[3] for r in rows)\n',
     'if r[0] in PF_PLACEHOLDERS]) == 2\n              and all(any(r[0] == sid and r[1:3] == ["1", "0"] and "Research" in r[3] and "placeholder" not in r[3] for r in rows) for sid in ("VALUE_QUALITY", "MOMENTUM"))\n'),
    ('    c.step("__click(\'#pui-more-body [data-pui-lab=\\"MOMENTUM\\"]\')", "!!document.querySelector(\'#pui-more-body [data-pui-lab-detail=\\"MOMENTUM\\"]\')", 30)\n',
     '    c.step("__click(\'#pui-more-body [data-pui-lab=\\"MEAN_REVERSION\\"]\')", "!!document.querySelector(\'#pui-more-body [data-pui-lab-detail=\\"MEAN_REVERSION\\"]\')", 30)   # Stage 6.5: MOMENTUM is implemented\n'),
]
GROUPS_EDITS_65 = [                                                                                # .github/ci/pytest_groups.json: tests/test_momentum_65.py in the research group (undone first)
    ('        "test_cross_sectional_641.py"\n      ]',
     '        "test_cross_sectional_641.py",\n        "test_momentum_65.py"\n      ]'),
]
GROUPS_EDITS_651 = [                                                                               # .github/ci/pytest_groups.json: tests/test_universe_integrity_651.py in the research group
    ('        "test_momentum_65.py"\n      ]',
     '        "test_momentum_65.py",\n        "test_universe_integrity_651.py"\n      ]'),
]
EDITS_65_612 = [                                                                                   # tests/test_product_flow_612.py: MOMENTUM implemented (research), two placeholders
    ('    for pid in ("MOMENTUM", "MEAN_REVERSION", "BREAKOUT"):\n        assert rows[pid]["implemented"] is False and rows[pid]["active"] is False and rows[pid]["status"] == "RESEARCH" and rows[pid]["setup_type"] is None and "not implemented" in rows[pid]["placeholder_note"]\n',
     '    mo = rows["MOMENTUM"]                                                                         # Stage 6.5: implemented as a RESEARCH strategy - never active, no setup type\n    assert mo["implemented"] is True and mo["active"] is False and mo["status"] == mo["declared_status"] == "RESEARCH" and mo["setup_type"] is None and mo["placeholder_note"] is None\n    for pid in ("MEAN_REVERSION", "BREAKOUT"):\n        assert rows[pid]["implemented"] is False and rows[pid]["active"] is False and rows[pid]["status"] == "RESEARCH" and rows[pid]["setup_type"] is None and "not implemented" in rows[pid]["placeholder_note"]\n'),
    ('j["counts"] == {"strategies": 5, "implemented": 2, "placeholders": 3}   # Stage 6.4: VALUE_QUALITY implemented\n',
     'j["counts"] == {"strategies": 5, "implemented": 3, "placeholders": 2}   # Stage 6.4: VALUE_QUALITY, Stage 6.5: MOMENTUM implemented\n'),
    ('[e.strategy_id for e in R.implemented()] == ["DOWNSIDE_DISLOCATION_RECOVERY", "VALUE_QUALITY"]   # + Stage 6.4\n',
     '[e.strategy_id for e in R.implemented()] == ["DOWNSIDE_DISLOCATION_RECOVERY", "VALUE_QUALITY", "MOMENTUM"]   # + Stage 6.4, 6.5\n'),
    ('            or (e.implemented and e.strategy_id == "VALUE_QUALITY" and e.status == "RESEARCH" and e.setup_type is None and not e.placeholder_note)   # Stage 6.4: a research strategy\n',
     '            or (e.implemented and e.strategy_id in ("VALUE_QUALITY", "MOMENTUM") and e.status == "RESEARCH" and e.setup_type is None and not e.placeholder_note)   # Stage 6.4 / 6.5: research strategies\n'),
    ('    assert v["counts"] == {"strategies": 5, "implemented": 2, "placeholders": 3} and [s["active"] for s in v["strategies"]] == [True, False, False, False, False]   # Stage 6.4\n',
     '    assert v["counts"] == {"strategies": 5, "implemented": 3, "placeholders": 2} and [s["active"] for s in v["strategies"]] == [True, False, False, False, False]   # Stage 6.4, 6.5\n'),
    ('"placeholder": (signal(strategy_id="MOMENTUM"), "STRATEGY_NOT_IMPLEMENTED")',
     '"placeholder": (signal(strategy_id="MEAN_REVERSION"), "STRATEGY_NOT_IMPLEMENTED")'),
    ('    for pid in ("MOMENTUM", "MEAN_REVERSION", "BREAKOUT"):\n        with pytest.raises(StrategyStandardError) as e:\n            validate_signal(signal(strategy_id=pid), reg)\n        assert e.value.code == "STRATEGY_NOT_IMPLEMENTED"                                        # a future placeholder never appears as an active signal\n    with pytest.raises(StrategyStandardError) as e:                                              # Stage 6.4: a RESEARCH strategy never emits a live signal either\n        validate_signal(signal(strategy_id="VALUE_QUALITY"), reg)\n    assert e.value.code == "STRATEGY_INACTIVE"\n',
     '    for pid in ("MEAN_REVERSION", "BREAKOUT"):\n        with pytest.raises(StrategyStandardError) as e:\n            validate_signal(signal(strategy_id=pid), reg)\n        assert e.value.code == "STRATEGY_NOT_IMPLEMENTED"                                        # a future placeholder never appears as an active signal\n    for pid in ("VALUE_QUALITY", "MOMENTUM"):                                                     # Stage 6.4 / 6.5: a RESEARCH strategy never emits a live signal either\n        with pytest.raises(StrategyStandardError) as e:\n            validate_signal(signal(strategy_id=pid), reg)\n        assert e.value.code == "STRATEGY_INACTIVE"\n'),
    ('    m = api.get("/api/strategy-registry/MOMENTUM").json()\n    assert m["entry"]["active"] is False and [x["state"] for x in m["sections"]] == ["DOCUMENTED"] + ["NOT_EVALUATED"] * 12 and all(x["note"].startswith("Not yet evaluated") for x in m["sections"][1:])\n',
     '    m = api.get("/api/strategy-registry/MEAN_REVERSION").json()                                   # Stage 6.5: MOMENTUM is implemented; a placeholder\n    assert m["entry"]["active"] is False and [x["state"] for x in m["sections"]] == ["DOCUMENTED"] + ["NOT_EVALUATED"] * 12 and all(x["note"].startswith("Not yet evaluated") for x in m["sections"][1:])\n    mo_d = api.get("/api/strategy-registry/MOMENTUM").json()                                      # Stage 6.5: documented from its frozen rules, never active\n    assert mo_d["entry"]["active"] is False and [x["state"] for x in mo_d["sections"]] == ["DOCUMENTED"] * 11 + ["NOT_EVALUATED"] * 2\n'),
]
# ---------------------------------------------------------------- Stage 6.6 (Mean Reversion): its exact in-place edits of earlier files
EDITS_66_REG = [                                                                                   # strategy_registry/registry.py: MEAN_REVERSION implemented (research)
    ('PLACEHOLDER_IDS = ("MEAN_REVERSION", "BREAKOUT")                  # Stage 6.4: VALUE_QUALITY, Stage 6.5: MOMENTUM are implemented (research only)\n',
     'PLACEHOLDER_IDS = ("BREAKOUT",)                                   # Stage 6.4: VALUE_QUALITY, 6.5: MOMENTUM, 6.6: MEAN_REVERSION are implemented (research only)\n'),
    ('    StrategyEntry(strategy_id="MEAN_REVERSION", display_name="Mean Reversion", source="INTERNAL", asset_class="US_EQUITY", direction_capability="LONG", time_horizon="SWING",\n                  status="RESEARCH", version="0.0", description="Future internal strategy (reversion to a reference level). " + PLACEHOLDER_NOTE,\n                  created_at=REGISTRY_DATE, updated_at=REGISTRY_DATE, implemented=False, placeholder_note=PLACEHOLDER_NOTE),\n',
     '    StrategyEntry(strategy_id="MEAN_REVERSION", display_name="Mean Reversion", source="INTERNAL", asset_class="US_EQUITY", direction_capability="LONG", time_horizon="SWING",\n                  status="RESEARCH", version="1.0",\n                  description=("Long-only weekly rebalance of the 20 stocks with the most negative 5-session adjusted return (short-term cross-sectional "\n                               "reversal), equal weight, from daily bars (Stage 6.6, mean_reversion/). Research only: it is validated historically "\n                               "against its eligible universe and never produces a trading signal."),\n                  created_at=REGISTRY_DATE, updated_at="2026-10-26", implemented=True),\n'),
]
EDITS_66_SR = [                                                                                    # strategy_registry/service.py: the Mean Reversion detail sections
    ('        sections, links = MOS.sections(), MOS.LINKS\n    else:\n',
     '        sections, links = MOS.sections(), MOS.LINKS\n    elif entry.implemented and entry.strategy_id == "MEAN_REVERSION":               # Stage 6.6: research strategy #4 (documented from its frozen rules)\n        from strategy_registry import mean_reversion as MRS\n        sections, links = MRS.sections(), MRS.LINKS\n    else:\n'),
]
EDITS_66_FLOWS = [                                                                                 # browser_tests/flows.py (outside the 6.2 block): one placeholder left
    ('PF_PLACEHOLDERS = ["MEAN_REVERSION", "BREAKOUT"]                                             # Stage 6.4 / 6.5: VALUE_QUALITY and MOMENTUM are implemented (research only)\n',
     'PF_PLACEHOLDERS = ["BREAKOUT"]                                                               # Stage 6.4 / 6.5 / 6.6: VALUE_QUALITY, MOMENTUM and MEAN_REVERSION are implemented (research only)\n'),
    ('    two placeholders marked placeholder · not active (Stage 6.4 / 6.5: Value + Quality and Momentum are implemented, research, not active), QuantConnect as a\n    supported source with\n',
     '    one placeholder marked placeholder · not active (Stage 6.4 / 6.5 / 6.6: Value + Quality, Momentum and Mean Reversion are implemented, research, not\n    active), QuantConnect as a supported source with\n'),
    ('    c.r.check("registry: the five entries in the API\'s order; Downside Dislocation Recovery implemented + active with its derived lifecycle label; the two placeholders not implemented, not active, marked; "\n              "Value + Quality and Momentum implemented, Research, not active, no placeholder mark",\n',
     '    c.r.check("registry: the five entries in the API\'s order; Downside Dislocation Recovery implemented + active with its derived lifecycle label; the placeholder not implemented, not active, marked; "\n              "Value + Quality, Momentum and Mean Reversion implemented, Research, not active, no placeholder mark",\n'),
    ('              and all(r[1] == "0" and r[2] == "0" and "placeholder · not active" in r[3] for r in rows if r[0] in PF_PLACEHOLDERS) and len([r for r in rows if r[0] in PF_PLACEHOLDERS]) == 2\n              and all(any(r[0] == sid and r[1:3] == ["1", "0"] and "Research" in r[3] and "placeholder" not in r[3] for r in rows) for sid in ("VALUE_QUALITY", "MOMENTUM"))\n',
     '              and all(r[1] == "0" and r[2] == "0" and "placeholder · not active" in r[3] for r in rows if r[0] in PF_PLACEHOLDERS) and len([r for r in rows if r[0] in PF_PLACEHOLDERS]) == 1\n              and all(any(r[0] == sid and r[1:3] == ["1", "0"] and "Research" in r[3] and "placeholder" not in r[3] for r in rows) for sid in ("VALUE_QUALITY", "MOMENTUM", "MEAN_REVERSION"))\n'),
    ('    c.step("__click(\'#pui-more-body [data-pui-lab=\\"MEAN_REVERSION\\"]\')", "!!document.querySelector(\'#pui-more-body [data-pui-lab-detail=\\"MEAN_REVERSION\\"]\')", 30)   # Stage 6.5: MOMENTUM is implemented\n',
     '    c.step("__click(\'#pui-more-body [data-pui-lab=\\"BREAKOUT\\"]\')", "!!document.querySelector(\'#pui-more-body [data-pui-lab-detail=\\"BREAKOUT\\"]\')", 30)   # Stage 6.5 / 6.6: MOMENTUM and MEAN_REVERSION are implemented\n'),
]
GROUPS_EDITS_66 = [                                                                                # .github/ci/pytest_groups.json: tests/test_mean_reversion_66.py in the research group
    ('        "test_universe_integrity_651.py"\n      ]',
     '        "test_universe_integrity_651.py",\n        "test_mean_reversion_66.py"\n      ]'),
]
EDITS_66_612 = [                                                                                   # tests/test_product_flow_612.py: MEAN_REVERSION implemented (research), one placeholder
    ('    for pid in ("MEAN_REVERSION", "BREAKOUT"):\n        assert rows[pid]["implemented"] is False and rows[pid]["active"] is False and rows[pid]["status"] == "RESEARCH" and rows[pid]["setup_type"] is None and "not implemented" in rows[pid]["placeholder_note"]\n',
     '    mr = rows["MEAN_REVERSION"]                                                                   # Stage 6.6: implemented as a RESEARCH strategy - never active, no setup type\n    assert mr["implemented"] is True and mr["active"] is False and mr["status"] == mr["declared_status"] == "RESEARCH" and mr["setup_type"] is None and mr["placeholder_note"] is None\n    for pid in ("BREAKOUT",):\n        assert rows[pid]["implemented"] is False and rows[pid]["active"] is False and rows[pid]["status"] == "RESEARCH" and rows[pid]["setup_type"] is None and "not implemented" in rows[pid]["placeholder_note"]\n'),
    ('j["counts"] == {"strategies": 5, "implemented": 3, "placeholders": 2}   # Stage 6.4: VALUE_QUALITY, Stage 6.5: MOMENTUM implemented\n',
     'j["counts"] == {"strategies": 5, "implemented": 4, "placeholders": 1}   # Stage 6.4: VALUE_QUALITY, 6.5: MOMENTUM, 6.6: MEAN_REVERSION implemented\n'),
    ('    m = api.get("/api/strategy-registry/MEAN_REVERSION").json()                                   # Stage 6.5: MOMENTUM is implemented; a placeholder\n',
     '    m = api.get("/api/strategy-registry/BREAKOUT").json()                                         # Stage 6.5 / 6.6: MOMENTUM and MEAN_REVERSION are implemented; a placeholder\n'),
    ('    assert mo_d["entry"]["active"] is False and [x["state"] for x in mo_d["sections"]] == ["DOCUMENTED"] * 11 + ["NOT_EVALUATED"] * 2\n',
     '    assert mo_d["entry"]["active"] is False and [x["state"] for x in mo_d["sections"]] == ["DOCUMENTED"] * 11 + ["NOT_EVALUATED"] * 2\n    mr_d = api.get("/api/strategy-registry/MEAN_REVERSION").json()                                # Stage 6.6: documented from its frozen rules, never active\n    assert mr_d["entry"]["active"] is False and [x["state"] for x in mr_d["sections"]] == ["DOCUMENTED"] * 11 + ["NOT_EVALUATED"] * 2\n'),
    ('[e.strategy_id for e in R.implemented()] == ["DOWNSIDE_DISLOCATION_RECOVERY", "VALUE_QUALITY", "MOMENTUM"]   # + Stage 6.4, 6.5\n',
     '[e.strategy_id for e in R.implemented()] == ["DOWNSIDE_DISLOCATION_RECOVERY", "VALUE_QUALITY", "MOMENTUM", "MEAN_REVERSION"]   # + Stage 6.4, 6.5, 6.6\n'),
    ('e.strategy_id in ("VALUE_QUALITY", "MOMENTUM") and e.status == "RESEARCH" and e.setup_type is None and not e.placeholder_note)   # Stage 6.4 / 6.5: research strategies\n',
     'e.strategy_id in ("VALUE_QUALITY", "MOMENTUM", "MEAN_REVERSION") and e.status == "RESEARCH" and e.setup_type is None and not e.placeholder_note)   # Stage 6.4 / 6.5 / 6.6: research strategies\n'),
    ('    assert v["counts"] == {"strategies": 5, "implemented": 3, "placeholders": 2} and [s["active"] for s in v["strategies"]] == [True, False, False, False, False]   # Stage 6.4, 6.5\n',
     '    assert v["counts"] == {"strategies": 5, "implemented": 4, "placeholders": 1} and [s["active"] for s in v["strategies"]] == [True, False, False, False, False]   # Stage 6.4, 6.5, 6.6\n'),
    ('"placeholder": (signal(strategy_id="MEAN_REVERSION"), "STRATEGY_NOT_IMPLEMENTED")',
     '"placeholder": (signal(strategy_id="BREAKOUT"), "STRATEGY_NOT_IMPLEMENTED")'),
    ('    for pid in ("MEAN_REVERSION", "BREAKOUT"):\n        with pytest.raises(StrategyStandardError) as e:\n',
     '    for pid in ("BREAKOUT",):\n        with pytest.raises(StrategyStandardError) as e:\n'),
    ('    for pid in ("VALUE_QUALITY", "MOMENTUM"):                                                     # Stage 6.4 / 6.5: a RESEARCH strategy never emits a live signal either\n',
     '    for pid in ("VALUE_QUALITY", "MOMENTUM", "MEAN_REVERSION"):                                   # Stage 6.4 / 6.5 / 6.6: a RESEARCH strategy never emits a live signal either\n'),
]
# ---------------------------------------------------------------- Stage 6.7 (Breakout): its exact in-place edits of earlier files
EDITS_67_REG = [                                                                                   # strategy_registry/registry.py: BREAKOUT implemented (research); no placeholder left
    ('PLACEHOLDER_IDS = ("BREAKOUT",)                                   # Stage 6.4: VALUE_QUALITY, 6.5: MOMENTUM, 6.6: MEAN_REVERSION are implemented (research only)\n',
     'PLACEHOLDER_IDS = ()                                              # Stage 6.4 / 6.5 / 6.6 / 6.7: every registry strategy is implemented (four research strategies)\n'),
    ('    StrategyEntry(strategy_id="BREAKOUT", display_name="Breakout", source="INTERNAL", asset_class="US_EQUITY", direction_capability="LONG", time_horizon="SWING",\n                  status="RESEARCH", version="0.0", description="Future internal strategy (range breakout). " + PLACEHOLDER_NOTE,\n                  created_at=REGISTRY_DATE, updated_at=REGISTRY_DATE, implemented=False, placeholder_note=PLACEHOLDER_NOTE),\n',
     '    StrategyEntry(strategy_id="BREAKOUT", display_name="Breakout", source="INTERNAL", asset_class="US_EQUITY", direction_capability="LONG", time_horizon="SWING",\n                  status="RESEARCH", version="1.0",\n                  description=("Long-only weekly rebalance of up to 20 stocks whose adjusted close broke above the highest adjusted high of the prior 20 "\n                               "sessions, the strongest breakouts first, at a fixed 5 % each with the unused slots in cash, from daily bars (Stage 6.7, "\n                               "breakout/). Research only: it is validated historically against its eligible universe and never produces a trading signal."),\n                  created_at=REGISTRY_DATE, updated_at="2026-10-26", implemented=True),\n'),
]
EDITS_67_SR = [                                                                                    # strategy_registry/service.py: the Breakout detail sections
    ('        sections, links = MRS.sections(), MRS.LINKS\n    else:\n',
     '        sections, links = MRS.sections(), MRS.LINKS\n    elif entry.implemented and entry.strategy_id == "BREAKOUT":                     # Stage 6.7: research strategy #5 (documented from its frozen rules)\n        from strategy_registry import breakout as BOS\n        sections, links = BOS.sections(), BOS.LINKS\n    else:\n'),
]
EDITS_67_FLOWS = [                                                                                 # browser_tests/flows.py (outside the 6.2 block): no placeholder left
    ('PF_PLACEHOLDERS = ["BREAKOUT"]                                                               # Stage 6.4 / 6.5 / 6.6: VALUE_QUALITY, MOMENTUM and MEAN_REVERSION are implemented (research only)\n',
     'PF_PLACEHOLDERS = []                                                                         # Stage 6.4 - 6.7: every registry strategy is implemented (research only)\n'),
    ('    one placeholder marked placeholder · not active (Stage 6.4 / 6.5 / 6.6: Value + Quality, Momentum and Mean Reversion are implemented, research, not\n    active), QuantConnect as a supported source with\n',
     '    no placeholder left (Stage 6.4 / 6.5 / 6.6 / 6.7: Value + Quality, Momentum, Mean Reversion and Breakout are implemented, research, not\n    active), QuantConnect as a supported source with\n'),
    ('    c.r.check("registry: the five entries in the API\'s order; Downside Dislocation Recovery implemented + active with its derived lifecycle label; the placeholder not implemented, not active, marked; "\n              "Value + Quality, Momentum and Mean Reversion implemented, Research, not active, no placeholder mark",\n',
     '    c.r.check("registry: the five entries in the API\'s order; Downside Dislocation Recovery implemented + active with its derived lifecycle label; no placeholder left; "\n              "Value + Quality, Momentum, Mean Reversion and Breakout implemented, Research, not active, no placeholder mark",\n'),
    ('              and all(r[1] == "0" and r[2] == "0" and "placeholder · not active" in r[3] for r in rows if r[0] in PF_PLACEHOLDERS) and len([r for r in rows if r[0] in PF_PLACEHOLDERS]) == 1\n              and all(any(r[0] == sid and r[1:3] == ["1", "0"] and "Research" in r[3] and "placeholder" not in r[3] for r in rows) for sid in ("VALUE_QUALITY", "MOMENTUM", "MEAN_REVERSION"))\n',
     '              and all(r[1] == "0" and r[2] == "0" and "placeholder · not active" in r[3] for r in rows if r[0] in PF_PLACEHOLDERS) and len([r for r in rows if r[0] in PF_PLACEHOLDERS]) == 0 and not any("placeholder" in r[3] for r in rows)\n              and all(any(r[0] == sid and r[1:3] == ["1", "0"] and "Research" in r[3] and "placeholder" not in r[3] for r in rows) for sid in ("VALUE_QUALITY", "MOMENTUM", "MEAN_REVERSION", "BREAKOUT"))\n'),
    ('    c.step("__click(\'#pui-more-body [data-pui-lab=\\"BREAKOUT\\"]\')", "!!document.querySelector(\'#pui-more-body [data-pui-lab-detail=\\"BREAKOUT\\"]\')", 30)   # Stage 6.5 / 6.6: MOMENTUM and MEAN_REVERSION are implemented\n    ph = c.js("__tvText(\'#pui-more-body [data-pui-lab-detail]\')") or ""\n    ph_secs = c.js("[...document.querySelectorAll(\'#pui-more-body [data-pui-section]\')].map((e) => e.dataset.puiSectionState)")\n    c.r.check("detail (placeholder): marked placeholder · not active, every section but the overview reads Not yet evaluated; it never appears as an active signal",\n              "placeholder · not active" in ph and ph_secs[0] == "DOCUMENTED" and all(s == "NOT_EVALUATED" for s in ph_secs[1:]) and len(ph_secs) == 13 and "Research" in ph, ph_secs)\n',
     '    c.step("__click(\'#pui-more-body [data-pui-lab=\\"BREAKOUT\\"]\')", "!!document.querySelector(\'#pui-more-body [data-pui-lab-detail=\\"BREAKOUT\\"]\')", 30)   # Stage 6.7: no placeholder left\n    ph = c.js("__tvText(\'#pui-more-body [data-pui-lab-detail]\')") or ""\n    ph_secs = c.js("[...document.querySelectorAll(\'#pui-more-body [data-pui-section]\')].map((e) => e.dataset.puiSectionState)")\n    c.r.check("detail (Breakout, research): documented from its frozen rules, never active; paper results and the decision log read Not yet evaluated; no placeholder mark",\n              "placeholder · not active" not in ph and ph_secs == ["DOCUMENTED"] * 11 + ["NOT_EVALUATED"] * 2 and "Research" in ph, ph_secs)\n'),
]
GROUPS_EDITS_67 = [                                                                                # .github/ci/pytest_groups.json: tests/test_breakout_67.py in the research group
    ('        "test_mean_reversion_66.py"\n      ]',
     '        "test_mean_reversion_66.py",\n        "test_breakout_67.py"\n      ]'),
]
EDITS_67_612 = [                                                                                   # tests/test_product_flow_612.py: BREAKOUT implemented (research), a synthetic placeholder
    ('    for pid in ("BREAKOUT",):\n        assert rows[pid]["implemented"] is False and rows[pid]["active"] is False and rows[pid]["status"] == "RESEARCH" and rows[pid]["setup_type"] is None and "not implemented" in rows[pid]["placeholder_note"]\n',
     '    bo = rows["BREAKOUT"]                                                                         # Stage 6.7: implemented as a RESEARCH strategy - never active, no setup type\n    assert bo["implemented"] is True and bo["active"] is False and bo["status"] == bo["declared_status"] == "RESEARCH" and bo["setup_type"] is None and bo["placeholder_note"] is None\n    assert all(r["implemented"] for r in rows.values())                                           # Stage 6.7: no placeholder left\n'),
    ('j["counts"] == {"strategies": 5, "implemented": 4, "placeholders": 1}   # Stage 6.4: VALUE_QUALITY, 6.5: MOMENTUM, 6.6: MEAN_REVERSION implemented\n',
     'j["counts"] == {"strategies": 5, "implemented": 5, "placeholders": 0}   # Stage 6.4: VALUE_QUALITY, 6.5: MOMENTUM, 6.6: MEAN_REVERSION, 6.7: BREAKOUT implemented\n'),
    ('    m = api.get("/api/strategy-registry/BREAKOUT").json()                                         # Stage 6.5 / 6.6: MOMENTUM and MEAN_REVERSION are implemented; a placeholder\n    assert m["entry"]["active"] is False and [x["state"] for x in m["sections"]] == ["DOCUMENTED"] + ["NOT_EVALUATED"] * 12 and all(x["note"].startswith("Not yet evaluated") for x in m["sections"][1:])\n',
     '    from strategy_registry import registry as R_, service as S_                                    # Stage 6.7: no placeholder left - a synthetic one keeps the path tested\n    m = [x.model_dump() for x in S_.placeholder_sections(R_.get("BREAKOUT").model_copy(update={"implemented": False, "placeholder_note": R_.PLACEHOLDER_NOTE}))]\n    assert [x["state"] for x in m] == ["DOCUMENTED"] + ["NOT_EVALUATED"] * 12 and all(x["note"].startswith("Not yet evaluated") for x in m[1:])\n    bo_d = api.get("/api/strategy-registry/BREAKOUT").json()                                      # Stage 6.7: documented from its frozen rules, never active\n    assert bo_d["entry"]["active"] is False and [x["state"] for x in bo_d["sections"]] == ["DOCUMENTED"] * 11 + ["NOT_EVALUATED"] * 2\n'),
    ('[e.strategy_id for e in R.implemented()] == ["DOWNSIDE_DISLOCATION_RECOVERY", "VALUE_QUALITY", "MOMENTUM", "MEAN_REVERSION"]   # + Stage 6.4, 6.5, 6.6\n',
     '[e.strategy_id for e in R.implemented()] == ["DOWNSIDE_DISLOCATION_RECOVERY", "VALUE_QUALITY", "MOMENTUM", "MEAN_REVERSION", "BREAKOUT"]   # + Stage 6.4 - 6.7\n'),
    ('e.strategy_id in ("VALUE_QUALITY", "MOMENTUM", "MEAN_REVERSION") and e.status == "RESEARCH" and e.setup_type is None and not e.placeholder_note)   # Stage 6.4 / 6.5 / 6.6: research strategies\n',
     'e.strategy_id in ("VALUE_QUALITY", "MOMENTUM", "MEAN_REVERSION", "BREAKOUT") and e.status == "RESEARCH" and e.setup_type is None and not e.placeholder_note)   # Stage 6.4 - 6.7: research strategies\n'),
    ('    assert v["counts"] == {"strategies": 5, "implemented": 4, "placeholders": 1} and [s["active"] for s in v["strategies"]] == [True, False, False, False, False]   # Stage 6.4, 6.5, 6.6\n',
     '    assert v["counts"] == {"strategies": 5, "implemented": 5, "placeholders": 0} and [s["active"] for s in v["strategies"]] == [True, False, False, False, False]   # Stage 6.4 - 6.7\n'),
    ('    reg = R.by_id()\n    sig = validate_signal(signal(), reg)\n',
     '    reg = R.by_id()\n    reg["FUTURE_X"] = reg["BREAKOUT"].model_copy(update={"strategy_id": "FUTURE_X", "implemented": False, "placeholder_note": R.PLACEHOLDER_NOTE})   # Stage 6.7: a synthetic placeholder\n    sig = validate_signal(signal(), reg)\n'),
    ('"placeholder": (signal(strategy_id="BREAKOUT"), "STRATEGY_NOT_IMPLEMENTED")',
     '"placeholder": (signal(strategy_id="FUTURE_X"), "STRATEGY_NOT_IMPLEMENTED")'),
    ('    for pid in ("BREAKOUT",):\n        with pytest.raises(StrategyStandardError) as e:\n',
     '    for pid in ("FUTURE_X",):\n        with pytest.raises(StrategyStandardError) as e:\n'),
    ('    for pid in ("VALUE_QUALITY", "MOMENTUM", "MEAN_REVERSION"):                                   # Stage 6.4 / 6.5 / 6.6: a RESEARCH strategy never emits a live signal either\n',
     '    for pid in ("VALUE_QUALITY", "MOMENTUM", "MEAN_REVERSION", "BREAKOUT"):                       # Stage 6.4 - 6.7: a RESEARCH strategy never emits a live signal either\n'),
]
EDITS_62_612 = EDITS_62_612_OWN + EDITS_64_612 + EDITS_65_612 + EDITS_66_612 + EDITS_67_612   # applied in this order (the WAL bootstrap fix test replays it)
UNDO_62 = {"strategy_registry/service.py": EDITS_62_SR + EDITS_64_SR + EDITS_65_SR + EDITS_66_SR + EDITS_67_SR, "api/routes/strategy_registry.py": EDITS_62_ROUTE, "frontend/product_ui.js": EDITS_62_JS,
           "browser_tests/flows.py": EDITS_62_FLOWS + EDITS_64_FLOWS + EDITS_65_FLOWS + EDITS_66_FLOWS + EDITS_67_FLOWS, "strategy_registry/registry.py": EDITS_64_REG + EDITS_65_REG + EDITS_66_REG + EDITS_67_REG,
           ".github/ci/pytest_groups.json": GROUPS_EDITS_62 + GROUPS_EDITS_631 + GROUPS_EDITS_64 + GROUPS_EDITS_641 + GROUPS_EDITS_65 + GROUPS_EDITS_651 + GROUPS_EDITS_66 + GROUPS_EDITS_67, "tests/test_forward_validation_61.py": EDITS_62_61, "tests/test_product_ui_611.py": EDITS_62_611,
           "tests/test_product_flow_612.py": EDITS_62_612, "tests/test_db_bootstrap_busy.py": EDITS_62_FIX}


def undo_62(rel: str, text: str) -> str:
    """`text` (a current file) without the Stage 6.2 changes: its delimited block removed, then its exact in-place edits undone (last first)."""
    text = ledger_view(rel, text)                                                                 # Stage 6.8: a registered file is read as on main first
    if rel in STRIP_62:
        assert len(re.findall(STRIP_62[rel], text)) == 1, rel
        text = re.sub(STRIP_62[rel], "", text)
    for old, new in reversed(UNDO_62.get(rel, [])):
        assert text.count(new) == 1, (rel, new[:80])                                              # the comparison with the base then proves the rest exact
        text = text.replace(new, old)
    return text


def run(name="GOOD", **kw):
    hooks = kw.pop("arm_hooks", None)
    fx = kw.pop("fx", {})
    ds, ad = F.fixture(name, **fx)
    if name == "CANDIDATE_MISMATCH":
        hooks = {"GATED": F.drop_first}
    return E.run_validation(ad, ds, created_at=AT, arm_hooks=hooks, **kw), ds, ad


def rec(r):
    return json.loads(r.model_dump_json())


# ================================================================ S candidates =================================================================

def test_S01_candidates_are_generated_once_with_a_content_identity_and_are_immutable():
    ds, ad = F.fixture("GOOD")
    calls = []
    orig = ad.candidates
    ad.candidates = lambda d: (calls.append(d.end), list(orig(d)))[1]
    r = E.run_validation(ad, ds, created_at=AT)
    assert calls[0] == ds.end and calls.count(ds.end) == 1                                       # the candidate set: generated ONCE, whatever the number of arms
    assert len(calls) == 7 and all(c < ds.end for c in calls[1:])                                # the point-in-time causality cuts: 5 sampled + the TRAIN end (never fed to an arm)
    c = r.candidates[0]
    assert c.candidate_id == CD.candidate_identity(c.strategy_id, c.strategy_version, c.symbol, c.direction, c.signal_date, c.entry_reference, c.invalidation_reference,
                                                   c.feature_snapshot_hash)
    assert c.feature_snapshot_hash == CD.feature_hash(c.features, c.feature_as_of) and len(c.candidate_id) == 32
    with pytest.raises(Exception):
        c.symbol = "XXX"                                                                         # frozen
    other = CD.candidate_identity(c.strategy_id, "1.1", c.symbol, c.direction, c.signal_date, c.entry_reference, c.invalidation_reference, c.feature_snapshot_hash)
    assert other != c.candidate_id                                                               # a new strategy_version is a new candidate
    assert r.candidate_count == 100 and len({x.candidate_id for x in r.candidates}) == 100 and r.candidate_set_hash == CD.candidate_set_hash([x.candidate_id for x in r.candidates])
    assert [x.signal_date for x in r.candidates] == sorted(x.signal_date for x in r.candidates)


def test_S02_every_candidate_is_a_valid_strategy_standard_v1_signal_and_an_execution_field_is_refused():
    from strategy_registry.standard import check_signal
    ds, ad = F.fixture("GOOD")
    raw = next(iter(ad.candidates(ds)))
    assert check_signal(CD.standard_signal(ad, raw))["valid"] is True
    bad = raw.model_copy(update={"features": {**raw.features, "qty": 10}})
    cands, skipped = CD.normalize(ad, [bad], ds)
    assert cands == () and skipped[0].reason == "STANDARD_V1_INVALID" and "EXECUTION_FIELD_FORBIDDEN" in skipped[0].detail


def test_S03_missing_critical_data_is_skipped_and_counted_never_zero():
    r, ds, ad = run("GOOD", fx={"missing": (0, 1, 2)})
    assert r.candidate_count == 97 and r.diagnostics["skips"]["candidate_skipped_missing_data"] == 3
    assert [s.reason for s in r.candidates_skipped] == ["MISSING_CRITICAL_FEATURE"] * 3 and all(s.detail == "demand" for s in r.candidates_skipped)
    assert all(c.features["demand"] == 70.0 for c in r.candidates)                               # no candidate carries a filled-in zero
    nan = RawCandidate(symbol="S000", signal_date=ds.sessions[0], entry_reference=100.0, invalidation_reference=95.0, features={"stretch": float("nan"), "demand": 1.0},
                       feature_as_of={"stretch": ds.sessions[0], "demand": ds.sessions[0]})
    _, sk = CD.normalize(ad, [nan], ds)
    assert sk[0].reason == "MISSING_CRITICAL_FEATURE" and sk[0].detail == "stretch"


def test_S04_duplicates_unsupported_direction_and_an_invalid_stop_are_skipped_explicitly():
    ds, ad = F.fixture("GOOD")
    raw = next(iter(ad.candidates(ds)))
    cands, sk = CD.normalize(ad, [raw, raw, raw.model_copy(update={"direction": "SHORT"}), raw.model_copy(update={"invalidation_reference": 101.0}),
                                  raw.model_copy(update={"symbol": "NOPE"})], ds)
    assert len(cands) == 1 and [s.reason for s in sk] == ["DUPLICATE_CANDIDATE", "UNSUPPORTED_DIRECTION", "INVALID_STOP", "SIGNAL_BAR_NOT_IN_DATASET"]


# ================================================================ A arms ========================================================================

def test_A01_the_three_arms_receive_the_identical_candidate_set():
    r, _, _ = run("GOOD", gates=[F.SymbolGate()], model=F.DemandModel())
    ids = [c.candidate_id for c in r.candidates]
    assert r.arms["RULES"].input_candidate_ids == ids == r.arms["GATED"].input_candidate_ids == r.arms["MODEL"].input_candidate_ids
    assert r.integrity["same_candidate_set"] is True and r.integrity["problems"] == [] and r.integrity["candidate_set_hash"] == r.candidate_set_hash
    g = r.arms["GATED"]
    assert sum(d.decision == "STAND_ASIDE" for d in g.decisions) == 10 and all(d.source == "test_symbol_gate" for d in g.decisions if d.decision == "STAND_ASIDE")
    assert len(g.trades) == 90 and len(r.arms["RULES"].trades) == 100 and len(r.arms["MODEL"].trades) == 67
    by = {t.candidate_id: t for t in r.arms["RULES"].trades}
    for arm in ("GATED", "MODEL"):                                                               # an accepted candidate simulates identically in every arm
        for t in r.arms[arm].trades:
            assert t.model_dump(exclude={"arm", "trade_id"}) == by[t.candidate_id].model_dump(exclude={"arm", "trade_id"})


def test_A02_no_gate_means_NO_ADDITIONAL_GATES_and_no_model_means_NOT_EVALUATED_not_zero():
    r, _, _ = run("GOOD")
    g, m = r.arms["GATED"], r.arms["MODEL"]
    assert g.status == "EVALUATED" and g.note.startswith("NO_ADDITIONAL_GATES") and g.metrics["ALL"] == r.arms["RULES"].metrics["ALL"]
    assert m.status == "NOT_EVALUATED" and m.decisions == [] and m.trades == [] and m.metrics == {} and m.diagnostics == {} and m.verdict["label"] == "Not evaluated" and m.verdict["verdict"] is None
    assert m.input_candidate_ids == r.arms["RULES"].input_candidate_ids
    d = SV.detail(rec(r), r.strategy_id)
    assert d["run"]["arms"]["MODEL"]["label"] == "Not evaluated" and d["run"]["arms"]["MODEL"]["OOS"] is None and d["run"]["arms"]["MODEL"]["candidate_count"] == 100


def test_A03_a_model_is_a_filter_only_it_cannot_change_a_trade_or_mutate_the_candidate():
    r, _, _ = run("GOOD", model=F.GreedyModel())
    assert r.arms["MODEL"].status == "INVALID" and "may only answer TAKE or STAND_ASIDE" in r.arms["MODEL"].note and r.verdict["verdict"] == "INVALID"
    r2, _, _ = run("GOOD", model=F.MutatingModel())
    assert r2.arms["MODEL"].status == "EVALUATED" and all(c.features["demand"] == 70.0 for c in r2.candidates)
    assert r2.candidate_set_hash == run("GOOD")[0].candidate_set_hash


def test_A04_a_candidate_set_mismatch_is_VALIDATION_INVALID_and_nothing_is_compared():
    r, _, _ = run("CANDIDATE_MISMATCH")
    assert r.integrity["same_candidate_set"] is False and any("GATED: input candidates differ" in p for p in r.integrity["problems"])
    assert r.verdict["verdict"] == "INVALID" and r.verdict["label"] == "VALIDATION INVALID" and r.verdict["reasons"][0]["code"] == "CANDIDATE_SET_MISMATCH"
    assert all(a.trades == [] and a.metrics == {} for a in r.arms.values())                    # no simulation, no comparative conclusion
    assert A.same_candidates(["a", "b"], [A.rules_arm([])])["same_candidate_set"] is False


# ================================================================ O split / OOS / sample size ==================================================

def test_O01_the_split_is_chronological_and_configurable_never_random():
    ds, _ = F.fixture("GOOD")
    s = split(ds, 0.7)
    assert s["method"] == "CHRONOLOGICAL" and s["train_sessions"] == int(len(ds.sessions) * 0.7) and s["train_end"] < s["oos_start"] and s["oos_end"] == ds.end
    assert split(ds, 0.7) == s and split(ds, 0.6)["train_end"] < s["train_end"]
    for bad in (0.3, 0.95, 1.0):
        with pytest.raises(DatasetError):
            split(ds, bad)
    r, _, _ = run("GOOD")
    assert all((t.segment == "TRAIN") == (t.signal_date <= r.train_end) for t in r.arms["RULES"].trades)
    assert r.config_snapshot["split"] == s and r.oos_sessions == s["oos_sessions"]


def test_O02_OOS_is_reported_on_its_own_and_does_not_depend_on_TRAIN():
    a = F.build(F.pattern(100, "WWWLL", "WLLLL"))
    b = F.build(F.pattern(100, "WWWWL", "WLLLL"))                                               # same OOS outcomes, different TRAIN
    ra = E.run_validation(F.FixtureAdapter(a[1]), a[0], created_at=AT)
    rb = E.run_validation(F.FixtureAdapter(b[1]), b[0], created_at=AT)
    oa, ob = ra.arms["RULES"].metrics["OOS"], rb.arms["RULES"].metrics["OOS"]
    assert oa == ob and ra.arms["RULES"].metrics["TRAIN"] != rb.arms["RULES"].metrics["TRAIN"]
    m = ra.arms["RULES"].metrics
    assert m["TRAIN"]["trade_count"] + m["OOS"]["trade_count"] == m["ALL"]["trade_count"] and abs(m["TRAIN"]["net_pnl"] + m["OOS"]["net_pnl"] - m["ALL"]["net_pnl"]) < 1e-6


def test_O03_sample_size_rules_give_INSUFFICIENT_DATA_with_the_real_counts():
    r, _, _ = run("TOO_SMALL")
    assert r.verdict["verdict"] == "INSUFFICIENT_DATA" and r.verdict["label"] == "INSUFFICIENT DATA"
    assert {x["code"] for x in r.verdict["reasons"]} >= {"SAMPLE_TOO_SMALL", "OOS_SAMPLE_TOO_SMALL", "OOS_WINDOW_TOO_SHORT"}
    assert r.arms["RULES"].metrics["ALL"]["trade_count"] == 10 and r.arms["RULES"].metrics["ALL"]["sharpe_per_trade"] is None   # reported as measured, nothing padded
    r2, _, _ = run("GOOD", train_ratio=0.9)                                                     # 70 OOS sessions but 10 OOS trades
    assert r2.verdict["verdict"] == "INSUFFICIENT_DATA" and [x["code"] for x in r2.verdict["reasons"]] == ["OOS_SAMPLE_TOO_SMALL"]
    assert (MIN_TRADES, MIN_OOS_TRADES, MIN_OOS_SESSIONS, T_STAT_WEAK) == (30, 20, 60, 2.0)


# ================================================================ M simulation / metrics =======================================================

def _one(outcome, friction=None, sizing=None, policy="NEXT_BAR_OPEN"):
    ds, plan = F.build([outcome])
    ad = F.FixtureAdapter(plan)
    cands, _ = CD.normalize(ad, ad.candidates(ds), ds)
    return SIM.simulate(cands[0], ds, arm="RULES", segment="TRAIN", entry_policy=policy, friction={**DEFAULT_FRICTION, **(friction or {})}, sizing={**DEFAULT_SIZING, **(sizing or {})})


def test_M01_causal_fill_next_bar_open_gross_slippage_fees_net_exact():
    t, skip = _one("W")
    assert skip is None and t.entry_date > t.signal_date and t.entry_raw == 100.0 and t.entry_fill == 100.05 and t.exit_reason == "TARGET" and t.exit_raw == 110.0
    assert t.qty == 99 and t.size_status == "RISK_SIZED" and t.gross_pnl == 990.0 and t.slippage_cost == round((0.05 + 110 * 0.0005) * 99, 6) and t.fees == 0.0
    assert t.net_pnl == round(990.0 - t.slippage_cost, 6) == 979.605 and t.exit_fill == round(110 * (1 - 0.0005), 6)
    assert t.achieved_risk == round(99 * (100.05 - 95.0), 6) and t.r_net == round(t.net_pnl / t.achieved_risk, 6) and t.r_gross == round(990.0 / t.achieved_risk, 6)
    f, _ = _one("W", friction={"commission_per_order": 1.0, "fee_per_share": 0.01})
    assert f.fees == round(2 * 1.0 + 2 * 0.01 * 99, 6) and f.net_pnl == round(f.gross_pnl - f.slippage_cost - f.fees, 6)
    z, _ = _one("W", friction={"slippage_bps_per_side": 0.0})
    assert z.slippage_cost == 0.0 and z.net_pnl == z.gross_pnl


def test_M02_stops_gaps_time_exit_data_end_and_entry_gap_through_the_stop():
    l, _ = _one("L")
    assert l.exit_reason == "STOP" and l.exit_raw == 95.0 and l.net_pnl < 0
    f, _ = _one("F")
    assert f.exit_reason == "TIME_EXIT" and f.holding_bars == 5 and f.exit_raw == 100.08 and f.gross_pnl > 0 > f.net_pnl
    ds, plan = F.build(["W"])
    bars = {s: [b.model_dump() for b in rows] for s, rows in ds.bars.items()}
    bars["S000"][2] = {**bars["S000"][2], "open": 94.0, "high": 96.0, "low": 93.0, "close": 95.5}   # day 2 gaps through the stop
    gap = Dataset(bars, kind="SYNTHETIC")
    cands, _ = CD.normalize(F.FixtureAdapter(plan), F.FixtureAdapter(plan).candidates(gap), gap)
    t, _ = SIM.simulate(cands[0], gap, arm="RULES", segment="TRAIN", entry_policy="NEXT_BAR_OPEN", friction=DEFAULT_FRICTION, sizing=DEFAULT_SIZING)
    assert t.exit_reason == "STOP_GAP" and t.exit_raw == 94.0                                    # the worse open, not the stop
    bars["S000"][1] = {**bars["S000"][1], "open": 94.5, "low": 94.0}
    g2 = Dataset(bars, kind="SYNTHETIC")
    _, skip = SIM.simulate(cands[0], g2, arm="RULES", segment="TRAIN", entry_policy="NEXT_BAR_OPEN", friction=DEFAULT_FRICTION, sizing=DEFAULT_SIZING)
    assert skip.reason == "ENTRY_GAP_THROUGH_STOP"
    short = Dataset({"S000": bars["S000"][:3]}, kind="SYNTHETIC")
    t3, _ = SIM.simulate(cands[0].model_copy(update={"target_reference": None}), short, arm="RULES", segment="TRAIN", entry_policy="NEXT_BAR_OPEN", friction=DEFAULT_FRICTION, sizing=DEFAULT_SIZING)
    assert t3 is None or t3.exit_reason in ("DATA_END", "STOP", "STOP_GAP")


def test_M02b_an_entry_opening_at_or_above_the_target_is_skipped_never_a_target_exit_below_the_fill():
    ds, plan = F.build(["W"])
    bars = {s: [b.model_dump() for b in rows] for s, rows in ds.bars.items()}
    bars["S000"][1] = {**bars["S000"][1], "open": 111.0, "high": 112.0, "low": 109.0, "close": 110.5}
    gap = Dataset(bars, kind="SYNTHETIC")
    cands, _ = CD.normalize(F.FixtureAdapter(plan), F.FixtureAdapter(plan).candidates(gap), gap)
    t, skip = SIM.simulate(cands[0], gap, arm="RULES", segment="TRAIN", entry_policy="NEXT_BAR_OPEN", friction=DEFAULT_FRICTION, sizing=DEFAULT_SIZING)
    assert t is None and skip.reason == "ENTRY_GAP_THROUGH_TARGET"
    r = E.run_validation(F.FixtureAdapter(plan), gap, created_at=AT, train_ratio=0.9)                   # the holding window inside TRAIN (Stage 6.3.1 purges a crossing one)
    assert r.arms["RULES"].trades == [] and [s.reason for s in r.arms["RULES"].simulation_skips] == ["ENTRY_GAP_THROUGH_TARGET"] and r.audit.status == "PASS"
    assert r.arms["RULES"].metrics["ALL"]["simulation_skipped_count"] == 1


def test_M03_ambiguous_bars_resolve_stop_first_and_are_counted_with_the_target_first_alternative():
    t, _ = _one("A")
    assert t.exit_reason == "STOP" and t.ambiguous_bar is True and t.alt_target_first_net is not None and t.alt_target_first_net > t.net_pnl
    r, _, _ = run("AMBIGUOUS")
    m = r.arms["RULES"].metrics["ALL"]
    assert m["ambiguous_bars_count"] == 20 and r.diagnostics["ambiguous"]["policy"] == "STOP_FIRST" and r.diagnostics["ambiguous"]["dependent"] is True
    assert r.execution_assumptions["ambiguous_bar_policy"] == "STOP_FIRST"


def test_M04_t_stat_expectancy_profit_factor_and_max_drawdown_by_hand():
    xs = [1.0, -1.0, 2.0, 0.5, -0.5]
    m = sum(xs) / 5
    sd = math.sqrt(sum((x - m) ** 2 for x in xs) / 4)
    assert abs(MX.t_stat(xs) - m / (sd / math.sqrt(5))) < 1e-12 and MX.t_stat([1.0]) is None and MX.t_stat([1.0, 1.0]) is None
    T = lambda net, r, d: SimpleNamespace(net_pnl=net, r_net=r, r_gross=r, gross_pnl=net, slippage_cost=0.0, fees=0.0, trade_return=net / 1000, holding_bars=2, exit_date=d,  # noqa: E731
                                          entry_date=d, candidate_id=d, symbol="X", exit_reason="TARGET", ambiguous_bar=False, size_status="RISK_SIZED", configured_risk=1.0, achieved_risk=1.0)
    trades = [T(100, 1, "2020-01-01"), T(-50, -0.5, "2020-01-02"), T(-80, -0.8, "2020-01-03"), T(200, 2, "2020-01-04")]
    out = MX.compute(trades, candidate_count=4, accepted_count=4, stand_aside_count=0, equity=1000.0)
    assert out["max_drawdown"] == 130.0 and out["max_drawdown_pct"] == round(130 / 1100 * 100, 4)
    assert out["expectancy_R"] == round((1 - 0.5 - 0.8 + 2) / 4, 6) and out["expectancy_usd"] == round(170 / 4, 6) and out["profit_factor"] == round(300 / 130, 6)
    assert out["win_rate"] == 0.5 and out["sharpe_per_trade"] is None and "not reported below" in out["sharpe_note"]
    wins = MX.compute(trades[:1], candidate_count=1, accepted_count=1, stand_aside_count=0, equity=1000.0)
    assert wins["profit_factor"] is None and wins["profit_factor_note"] == "no losing trade"
    empty = MX.compute([], candidate_count=0, accepted_count=0, stand_aside_count=0, equity=1000.0)
    assert empty["trade_count"] == 0 and empty["expectancy_R"] is None and empty["win_rate"] is None and empty["mean_R_t_stat"] is None   # no trade: None, never 0


def test_M05_risk_based_sizing_reports_configured_vs_achieved_risk():
    t, _ = _one("W", sizing={"risk_fraction": 0.00001})
    assert t.qty == 1 and t.size_status == "MIN_ONE_UNIT" and t.achieved_risk > t.configured_risk
    c, _ = _one("W", sizing={"max_position_pct": 1.0})
    assert c.size_status == "CAPPED" and c.achieved_risk < c.configured_risk
    r, _, _ = run("GOOD")
    m = r.arms["RULES"].metrics["ALL"]
    assert m["size_status"] == {"RISK_SIZED": 100} and m["configured_risk_total"] == 50000.0 and m["achieved_risk_total"] > 0


def test_M06_every_required_metric_is_present_per_arm_and_per_segment():
    r, _, _ = run("GOOD")
    need = ("trade_count", "candidate_count", "accepted_count", "stand_aside_count", "win_rate", "gross_pnl", "slippage_cost", "fees", "net_pnl", "average_trade_return",
            "average_R", "sum_R", "expectancy_R", "expectancy_usd", "profit_factor", "max_drawdown", "max_drawdown_pct", "sharpe_per_trade", "average_holding_bars", "best_trade",
            "worst_trade", "exit_reason_distribution", "mean_R", "std_R", "mean_R_t_stat", "sample_size", "ambiguous_bars_count", "configured_risk_total", "achieved_risk_total")
    for arm in ("RULES", "GATED"):
        for seg in ("TRAIN", "OOS", "ALL"):
            assert all(k in r.arms[arm].metrics[seg] for k in need), (arm, seg)
    assert r.arms["RULES"].metrics["OOS"]["trade_count"] == 30 and r.arms["RULES"].metrics["TRAIN"]["trade_count"] == 70


# ================================================================ D diagnostics ================================================================

def test_D01_train_vs_oos_diagnosis():
    tv = lambda t, o: DG.train_vs_oos({"trade_count": 10, "expectancy_R": t}, {"trade_count": 10, "expectancy_R": o})["code"]  # noqa: E731
    assert (tv(0.5, 0.5), tv(0.5, 0.1), tv(0.5, 0.0), tv(0.5, -0.4), tv(-0.2, 0.4), tv(-0.2, -0.3)) == (
        "CONSISTENT", "DEGRADATION", "DEGRADATION", "FAILURE_INSTABILITY", "TRAIN_WEAK_OOS_POSITIVE", "CONSISTENTLY_WEAK")
    assert DG.train_vs_oos({"trade_count": 0, "expectancy_R": None}, {"trade_count": 3, "expectancy_R": 1})["code"] == "NO_COMPARISON"
    assert not any(re.search(r"(?i)regime", x["text"]) for x in (DG.train_vs_oos({"trade_count": 1, "expectancy_R": -1}, {"trade_count": 1, "expectancy_R": 1}),))


def test_D02_parameter_sensitivity_is_diagnostic_only_robust_sensitive_unstable():
    base, _, _ = run("GOOD")
    rob, _, _ = run("GOOD", neighbours=[{"keep": "mod:5:0,1,2,3,4"}], adapter_factory=None)
    assert rob.diagnostics["sensitivity"]["label"] == "NOT_RUN"                                    # no factory: nothing is run, nothing invented
    ds, ad = F.fixture("GOOD")
    r = E.run_validation(ad, ds, created_at=AT, neighbours=[{"keep": "mod:5:0,1,2,3,4"}], adapter_factory=ad.with_params)
    assert r.diagnostics["sensitivity"]["label"] == "ROBUST" and r.verdict["verdict"] == "PASS"
    s = E.run_validation(ad, ds, created_at=AT, neighbours=[{"keep": "mod:5:0,1,2"}], adapter_factory=ad.with_params)
    assert s.diagnostics["sensitivity"]["label"] == "SENSITIVE" and s.verdict["verdict"] == "MIXED" and "PARAMETERS_SENSITIVE" in [x["code"] for x in s.verdict["reasons"]]
    u = E.run_validation(ad, ds, created_at=AT, neighbours=[{"keep": "mod:5:3,4"}], adapter_factory=ad.with_params)
    assert u.diagnostics["sensitivity"]["label"] == "UNSTABLE"
    for x in (r, s, u):                                                                           # the base strategy, its candidates and its metrics never change
        assert x.candidate_set_hash == base.candidate_set_hash and x.arms["RULES"].metrics == base.arms["RULES"].metrics and ad.params == {}


def test_D03_period_concentration_by_year_quarter_month_without_regime_labels():
    T = lambda net, d: SimpleNamespace(net_pnl=net, exit_date=d)  # noqa: E731
    sev = DG.concentration([T(900, "2019-03-01"), T(50, "2020-06-01"), T(50, "2021-06-01")])
    assert sev["severe"] is True and "2019" in sev["text"] and sev["levels"]["year"]["top_share"] == 0.9
    ok = DG.concentration([T(100, "2019-03-01"), T(100, "2020-06-01"), T(100, "2021-06-01")])
    assert ok["severe"] is False and ok["warning"] is False
    assert DG.concentration([T(-5, "2019-01-01")])["text"].startswith("Total net P/L is not positive")
    src = (ROOT / "strategy_validation" / "diagnostics.py").read_text(encoding="utf-8")
    assert not re.search(r"(?i)\b(bull|bear|risk-on|risk-off)\b", src)


def test_D04_the_train_oos_boundary_is_purged_not_merely_reported():
    r, _, _ = run("GOOD")                                                                        # Stage 6.3.1: PURGE (tests/test_validation_integrity_631.py)
    b = r.diagnostics["boundary"]
    assert set(b) == {"policy", "train_boundary_purged_count", "text"} and b["policy"] == "PURGE" and b["train_boundary_purged_count"] == 0
    assert all(t.exit_date <= r.train_end for a in r.arms.values() for t in a.trades if t.segment == "TRAIN")   # no TRAIN trade uses an OOS bar


# ================================================================ U mechanical audit ===========================================================

def _audit(r, ds, mutate):
    d = rec(r)
    trades = [t for a in d["arms"].values() for t in a["trades"]]
    trades = mutate(d, trades) or trades
    bars = {s: [b.model_dump() for b in rows] for s, rows in ds.bars.items()}
    return AU.audit(d, d["candidates"], trades, bars)


def test_U01_the_audit_validates_every_trade_independently():
    r, ds, _ = run("GOOD")
    assert r.audit.status == "PASS" and r.audit.checked == 200 == r.audit.validated and r.audit.note == "200 / 200 trades validated independently"
    tree = ast.parse((ROOT / "strategy_validation" / "audit.py").read_text(encoding="utf-8"))
    mods = {n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)} | {a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
    assert not any(m and m.startswith("strategy_validation") for m in mods), mods                 # it shares no code with the simulator or the engine


def test_U02_the_audit_catches_duplicates_trades_before_the_signal_version_mismatch_and_a_wrong_exit():
    r, ds, _ = run("GOOD")
    dup = _audit(r, ds, lambda d, ts: ts + [dict(ts[0])])
    assert dup["status"] == "FAIL" and "DUPLICATE_EXECUTION" in [v["check"] for v in dup["violations"]]

    def early(d, ts):
        ts[0] = {**ts[0], "entry_date": ts[0]["signal_date"]}
    e = _audit(r, ds, early)
    assert {"TRADE_BEFORE_SIGNAL", "ENTRY_RULE"} <= {v["check"] for v in e["violations"]}

    def version(d, ts):
        ts[1] = {**ts[1], "strategy_version": "9.9"}
    assert "STRATEGY_VERSION" in [v["check"] for v in _audit(r, ds, version)["violations"]]

    def late(d, ts):
        i = next(k for k, t in enumerate(ts) if t["exit_reason"] == "STOP")                     # a stop recorded one bar later than it was touched
        bars = ds.bars[ts[i]["symbol"]]
        later = next(b for b in bars if b.date > ts[i]["exit_date"])
        ts[i] = {**ts[i], "exit_date": later.date, "holding_bars": ts[i]["holding_bars"] + 1}
    assert "EXIT_FIRST_TOUCH" in [v["check"] for v in _audit(r, ds, late)["violations"]]

    def seg(d, ts):
        i = next(k for k, t in enumerate(ts) if t["segment"] == "OOS")
        ts[i] = {**ts[i], "segment": "TRAIN"}                                                     # an OOS trade relabelled TRAIN
    assert "SEGMENT" in [v["check"] for v in _audit(r, ds, seg)["violations"]]

    def ident(d, ts):
        d["candidates"][0] = {**d["candidates"][0], "entry_reference": 101.0}
    assert "CANDIDATE_IDENTITY" in [v["check"] for v in _audit(r, ds, ident)["violations"]]

    def money(d, ts):
        ts[2] = {**ts[2], "net_pnl": ts[2]["net_pnl"] + 5}
    assert "FRICTION" in [v["check"] for v in _audit(r, ds, money)["violations"]]

    def ghost(d, ts):
        ts.append({**ts[0], "candidate_id": "f" * 32, "trade_id": "x"})
    assert "CANDIDATE_EXISTS" in [v["check"] for v in _audit(r, ds, ghost)["violations"]]


def test_U03_the_audit_checks_trades_against_the_arm_decisions_completeness_and_the_size():
    r, ds, _ = run("GOOD", gates=[F.SymbolGate()])
    assert r.audit.status == "PASS" and r.audit.checked == 190

    def vetoed(d, ts):
        cid = next(x["candidate_id"] for x in d["arms"]["GATED"]["decisions"] if x["decision"] == "STAND_ASIDE")
        src = next(t for t in ts if t["arm"] == "RULES" and t["candidate_id"] == cid)
        ts.append({**src, "arm": "GATED", "trade_id": "g" * 24})                              # a vetoed candidate that still traded
    assert "NOT_ACCEPTED" in [v["check"] for v in _audit(r, ds, vetoed)["violations"]]

    def dropped(d, ts):
        i = next(k for k, t in enumerate(ts) if t["arm"] == "RULES" and t["net_pnl"] < 0)
        del ts[i]                                                                                 # a losing trade silently removed
    assert "MISSING_TRADE" in [v["check"] for v in _audit(r, ds, dropped)["violations"]]

    def size(d, ts):
        ts[0] = {**ts[0], "qty": ts[0]["qty"] + 1}
    assert "SIZE" in [v["check"] for v in _audit(r, ds, size)["violations"]]


def test_U04_a_failed_audit_makes_the_run_INVALID():
    v = __import__("strategy_validation.verdict", fromlist=["decide"])
    r, _, _ = run("GOOD")
    out = v.decide(r.arms["RULES"].metrics, oos_sessions=r.oos_sessions, integrity_ok=True, integrity_problems=[], causality_critical=False, audit_ok=False, diagnostics=r.diagnostics)
    assert out["verdict"] == "INVALID" and out["reasons"][0]["code"] == "AUDIT_FAILED"


# ================================================================ L look-ahead / causality =====================================================

def test_L01_look_ahead_is_flagged_and_makes_the_run_INVALID_manual_review_always_required():
    r, _, _ = run("LOOKAHEAD")
    checks = {c["check"]: c["result"] for c in r.causality.automated_checks}
    assert checks["STATIC_FUTURE_SHIFT"] == "CRITICAL" and checks["RUNTIME_FEATURE_AFTER_SIGNAL"] == "CRITICAL" and r.causality.critical is True
    assert r.verdict["verdict"] == "INVALID" and "CAUSALITY_VIOLATION" in [x["code"] for x in r.verdict["reasons"]]
    g, _, _ = run("GOOD")
    assert g.causality.critical is False and g.causality.manual_review_required is True and g.causality.manual_review_note.startswith("MANUAL_CAUSALITY_REVIEW_REQUIRED")
    assert {c["kind"] for c in g.causality.automated_checks} == {"AUTOMATED", "STRUCTURAL"}
    assert [c["result"] for c in CA.static_scan("x = df['close'].rolling(5, center=True).mean()")] .count("CRITICAL") == 1


def test_L01b_a_selection_on_future_bars_is_caught_by_the_point_in_time_truncation_check():
    ds, plan = F.fixture("GOOD")[0], F.fixture("GOOD")[1].plan
    r = E.run_validation(F.PeekingAdapter(plan), ds, created_at=AT)
    checks = {c["check"]: c for c in r.causality.automated_checks}
    assert checks["RUNTIME_FEATURE_AFTER_SIGNAL"]["result"] == "PASS"                             # no dated feature reveals it
    assert checks["RUNTIME_TRUNCATION_INVARIANCE"]["result"] == "CRITICAL" and r.verdict["verdict"] == "INVALID" and "CAUSALITY_VIOLATION" in [x["code"] for x in r.verdict["reasons"]]
    g, _, _ = run("GOOD")
    t = {c["check"]: c for c in g.causality.automated_checks}["RUNTIME_TRUNCATION_INVARIANCE"]
    assert t["result"] == "PASS" and "point-in-time cuts" in t["detail"]
    d = g.config_snapshot and F.fixture("GOOD")[0]
    cut = d.until(d.sessions[10])
    assert cut.end == d.sessions[10] and all(b.date <= d.sessions[10] for rows in cut.bars.values() for b in rows) and d.end > cut.end


def test_L02_an_entry_at_the_signal_close_needs_a_declared_market_on_close_assumption():
    ds, ad = F.fixture("GOOD")
    ad.entry_policy = "SIGNAL_CLOSE_MOC"
    r = E.run_validation(ad, ds, created_at=AT)
    assert {c["check"]: c["result"] for c in r.causality.automated_checks}["RUNTIME_ENTRY_AT_SIGNAL_CLOSE"] == "CRITICAL" and r.verdict["verdict"] == "INVALID"
    ad.moc_declared = True
    r2 = E.run_validation(ad, ds, created_at=AT)
    assert r2.causality.critical is False and all(t.entry_date == t.signal_date and t.exit_date > t.entry_date for t in r2.arms["RULES"].trades) and r2.audit.status == "PASS"


# ================================================================ V verdicts ===================================================================

@pytest.mark.parametrize("name, verdict, code", [("GOOD", "PASS", "ALL_CHECKS_PASSED"), ("OVERFIT", "FAIL", "OOS_NOT_POSITIVE"), ("TOO_SMALL", "INSUFFICIENT_DATA", "SAMPLE_TOO_SMALL"),
                                                 ("FRICTION_KILLED", "FAIL", "FRICTION_KILLED"), ("AMBIGUOUS", "MIXED", "AMBIGUOUS_BAR_DEPENDENCE"),
                                                 ("LOOKAHEAD", "INVALID", "CAUSALITY_VIOLATION"), ("CANDIDATE_MISMATCH", "INVALID", "CANDIDATE_SET_MISMATCH")])
def test_V01_the_fixture_verdicts(name, verdict, code):
    r, _, _ = run(name)
    assert r.verdict["verdict"] == verdict and code in [x["code"] for x in r.verdict["reasons"]] and r.verdict["label"] == VERDICT_LABELS[verdict]
    assert r.verdict["eligible_for_review"] is (verdict == "PASS") and r.verdict["decision"] == ("Eligible for review" if verdict == "PASS" else "Not ready for paper")
    assert r.verdict["lifecycle_effect"].startswith("none")
    if verdict == "FAIL":
        assert r.verdict["note"].startswith("This strategy should not progress to paper trading.")
    if verdict == "PASS":
        assert "does not promote" in r.verdict["note"] and "does not mean future profitability" in r.verdict["note"]


def test_V02_friction_killed_shows_gross_positive_and_net_negative():
    r, _, _ = run("FRICTION_KILLED")
    m = r.arms["RULES"].metrics["ALL"]
    assert m["gross_pnl"] > 0 > m["net_pnl"] and r.diagnostics["friction"]["killed"] is True and m["slippage_cost"] > m["gross_pnl"]


def test_V03_weak_t_and_degradation_are_MIXED_not_PASS():
    ds, plan = F.build(F.pattern(120, "WLL", "WLLWLL"))                                          # ~+0.3R per trade but noisy
    r = E.run_validation(F.FixtureAdapter(plan), ds, created_at=AT)
    t = r.arms["RULES"].metrics["ALL"]["mean_R_t_stat"]
    assert r.verdict["verdict"] in ("MIXED", "FAIL") and (r.verdict["verdict"] == "FAIL" or abs(t) < T_STAT_WEAK or "DEGRADATION" in [x["code"] for x in r.verdict["reasons"]])
    d2, p2 = F.build(F.pattern(100, "WWWWL", "WWWLLLWWLL"))                                      # TRAIN strong, OOS positive but far weaker
    r2 = E.run_validation(F.FixtureAdapter(p2), d2, created_at=AT)
    assert r2.verdict["verdict"] == "MIXED" and "DEGRADATION" in [x["code"] for x in r2.verdict["reasons"]]


# ================================================================ R registry / store / routes ==================================================

def test_R01_registry_rows_carry_the_validation_fields_and_downside_recovery_is_NOT_RUN_without_fake_results():
    from strategy_registry import registry as R
    from strategy_registry import service as SR
    from strategy_validation.adapters import DDR_REASON, PLACEHOLDER_REASON, DownsideRecoveryAdapter, adapter_for
    v = SR.registry_view()
    fields = ("validation_status", "latest_validation_run_id", "validation_verdict", "train_trade_count", "oos_trade_count", "mechanical_audit_status", "last_validated_at")
    for row in v["strategies"]:
        assert all(k in row for k in fields) and row["validation_status"] == "NOT_RUN" and row["validation_label"] == "Not yet evaluated" and row["train_trade_count"] is None
    rows = {r["strategy_id"]: r for r in v["strategies"]}
    assert rows["DOWNSIDE_DISLOCATION_RECOVERY"]["not_run_reason"] == DDR_REASON and adapter_for("BREAKOUT", R.get("BREAKOUT").model_copy(update={"implemented": False}))[1] == PLACEHOLDER_REASON   # Stage 6.7: no placeholder left - a synthetic one
    assert adapter_for("DOWNSIDE_DISLOCATION_RECOVERY", R.get("DOWNSIDE_DISLOCATION_RECOVERY")) == (None, DDR_REASON)
    with pytest.raises(E.ValidationError):
        E.run_validation(DownsideRecoveryAdapter(), F.fixture("GOOD")[0])                         # no honest replay: the harness refuses instead of inventing one
    d = SR.detail_view("DOWNSIDE_DISLOCATION_RECOVERY")
    assert d["validation"]["run"] is None and d["validation"]["validation_status"] == "NOT_RUN" and d["entry"]["validation_status"] == "NOT_RUN"
    assert {s["key"]: s["state"] for s in d["sections"]}["backtest"] == "NOT_EVALUATED"           # the Stage 6.1.2 backtest section is unchanged


def test_R02_a_stored_verdict_never_changes_a_lifecycle(tmp_path, monkeypatch):
    from strategy_registry import registry as R
    from strategy_registry import service as SR
    from strategy_registry.models import StrategyEntry
    from strategy_validation.store import StrategyValidationStore
    monkeypatch.setenv("STOCK_AGENT_ENV", "test")
    before = [(e.strategy_id, e.status) for e in R.entries()]
    ddr = R.get("DOWNSIDE_DISLOCATION_RECOVERY")
    fx = StrategyEntry(**{**ddr.model_dump(), "strategy_id": "FIXTURE_GOOD", "display_name": "Fixture good", "status": "RESEARCH", "setup_type": None})
    monkeypatch.setattr(R, "ENTRIES", tuple(R.ENTRIES) + (fx,))
    store = StrategyValidationStore(tmp_path / "v.db")
    r, _, _ = run("GOOD")
    store.insert(r)
    summaries = SV.registry_summaries(store, R.entries())
    rows = {x["strategy_id"]: x for x in SR.registry_view(None, summaries)["strategies"]}
    assert rows["FIXTURE_GOOD"]["validation_status"] == "PASS" and rows["FIXTURE_GOOD"]["status"] == "RESEARCH" and rows["FIXTURE_GOOD"]["oos_trade_count"] == 30
    assert rows["FIXTURE_GOOD"]["validation_synthetic"] is True and rows["DOWNSIDE_DISLOCATION_RECOVERY"]["status"] in ("PAPER_CANDIDATE", "PAPER_ACTIVE")
    monkeypatch.setattr(R, "ENTRIES", tuple(e for e in R.ENTRIES if e.strategy_id != "FIXTURE_GOOD"))
    assert [(e.strategy_id, e.status) for e in R.entries()] == before
    src = "".join((ROOT / "strategy_validation" / f).read_text(encoding="utf-8") for f in os.listdir(ROOT / "strategy_validation") if f.endswith(".py"))
    assert not re.search(r"\.status\s*=(?!=)|ENTRIES\s*=(?!=)|setattr\(|lifecycle_status\s*=(?!=)", src)         # nothing in the harness writes a lifecycle


def test_R03_the_store_is_append_only_and_synthetic_runs_are_test_only(tmp_path, monkeypatch):
    from strategy_validation.store import StoreError, StrategyValidationStore
    db = tmp_path / "v.db"
    r, _, _ = run("GOOD")
    monkeypatch.delenv("STOCK_AGENT_ENV", raising=False)
    with pytest.raises(StoreError, match="refused outside a test process"):
        StrategyValidationStore(db).insert(r)
    monkeypatch.setenv("STOCK_AGENT_ENV", "test")
    s = StrategyValidationStore(db)
    s.insert(r)
    got = s.run(r.run_id)
    assert got["verdict"]["verdict"] == "PASS" and got["candidate_set_hash"] == r.candidate_set_hash and "candidates" not in got
    assert len(s.candidates(r.run_id)) == 100 and len(s.trades(r.run_id)) == 200 and len(s.trades(r.run_id, "GATED")) == 100 and s.latest("FIXTURE_GOOD")["run_id"] == r.run_id
    with sqlite3.connect(str(db)) as conn:
        for sql in ("UPDATE strategy_validation_runs SET verdict = 'FAIL'", "DELETE FROM strategy_validation_trades", "UPDATE strategy_validation_candidates SET symbol = 'X'"):
            with pytest.raises(sqlite3.IntegrityError):
                conn.execute(sql)
    with pytest.raises(sqlite3.IntegrityError):
        s.insert(r)                                                                               # one run id, written once
    monkeypatch.delenv("STOCK_AGENT_ENV")
    assert s.run(r.run_id) is None and s.runs() == [] and s.latest("FIXTURE_GOOD") is None        # invisible to a normal process
    incomplete = r.model_copy(update={"verdict": {}})
    monkeypatch.setenv("STOCK_AGENT_ENV", "test")
    with pytest.raises(StoreError, match="complete run"):
        s.insert(incomplete)
    assert StrategyValidationStore(tmp_path / "none.db").runs() == []


def test_R03b_a_parameter_change_under_the_same_strategy_version_is_refused(tmp_path, monkeypatch):
    from strategy_validation.store import StoreError, StrategyValidationStore
    monkeypatch.setenv("STOCK_AGENT_ENV", "test")
    s = StrategyValidationStore(tmp_path / "v.db")
    ds, ad = F.fixture("GOOD")
    s.insert(E.run_validation(ad, ds, created_at=AT))
    s.insert(E.run_validation(ad, ds, created_at=AT))                                            # the same configuration again: a reproducible re-run
    changed = ad.with_params({"keep": "mod:5:0,1,2"})
    with pytest.raises(StoreError, match="a change after a validation is a new strategy_version"):
        s.insert(E.run_validation(changed, ds, created_at=AT))
    changed.strategy_version = "1.1"
    s.insert(E.run_validation(changed, ds, created_at=AT))                                       # a new version is a new strategy
    assert [r["strategy_version"] for r in s.runs("FIXTURE_GOOD")].count("1.1") == 1 and len(s.runs("FIXTURE_GOOD")) == 3


@pytest.fixture
def api(tmp_path, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    import database.database as dbm
    import strategy_validation.store as ST
    monkeypatch.setattr(dbm, "_db_instance", dbm.ResearchDatabase(str(tmp_path / "app.db")))
    monkeypatch.setattr(ST, "default_path", lambda: tmp_path / "app.db")
    from api.routes import strategy_registry
    app = FastAPI()
    app.include_router(strategy_registry.router)
    return SimpleNamespace(client=TestClient(app), app=app, db=tmp_path / "app.db")


def test_R04_the_validation_routes_are_GET_only_and_read_the_store(api, monkeypatch):
    from strategy_registry import registry as R
    from strategy_registry.models import StrategyEntry
    from strategy_validation.store import StrategyValidationStore
    paths = {p: set(o) for p, o in api.app.openapi()["paths"].items() if p.startswith("/api/strategy-registry")}
    assert paths == {"/api/strategy-registry": {"get"}, "/api/strategy-registry/standard": {"get"}, "/api/strategy-registry/{strategy_id}": {"get"},
                     "/api/strategy-registry/{strategy_id}/validation": {"get"}, "/api/strategy-registry/validation/runs/{run_id}": {"get"},
                     "/api/strategy-registry/validation/runs/{run_id}/simulations": {"get"}, "/api/strategy-registry/validation/runs/{run_id}/audit": {"get"}}
    for p in paths:
        assert not re.search(r"(?i)order|execute|broker|place|cancel|submit|buy|sell|preview|trade|alpaca|robinhood|paper|tune|optimi|backtest|launch|start", p), p
    c = api.client
    d = c.get("/api/strategy-registry/DOWNSIDE_DISLOCATION_RECOVERY/validation").json()
    assert d["validation_status"] == "NOT_RUN" and d["run"] is None and d["declared_status"] == "PAPER_CANDIDATE" and "No honest historical replay" in d["not_run_reason"]
    assert c.post("/api/strategy-registry/DOWNSIDE_DISLOCATION_RECOVERY/validation").status_code == 405
    assert c.get("/api/strategy-registry/NOPE_X/validation").status_code == 404 and c.get("/api/strategy-registry/x!/validation").status_code == 400
    assert c.get("/api/strategy-registry/validation/runs/abc").status_code == 400 and c.get("/api/strategy-registry/validation/runs/" + "0" * 32).status_code == 404
    monkeypatch.setenv("STOCK_AGENT_ENV", "test")
    ddr = R.get("DOWNSIDE_DISLOCATION_RECOVERY")
    monkeypatch.setattr(R, "ENTRIES", tuple(R.ENTRIES) + (StrategyEntry(**{**ddr.model_dump(), "strategy_id": "FIXTURE_GOOD", "display_name": "Fixture good", "status": "RESEARCH", "setup_type": None}),))
    r, _, _ = run("GOOD", gates=[F.SymbolGate()])
    StrategyValidationStore(api.db).insert(r)
    det = c.get("/api/strategy-registry/FIXTURE_GOOD").json()
    assert det["validation"]["validation_status"] == "PASS" and det["entry"]["status"] == "RESEARCH" and det["validation"]["run"]["arms"]["GATED"]["candidate_count"] == 100
    reg = {x["strategy_id"]: x for x in c.get("/api/strategy-registry").json()["strategies"]}
    assert reg["FIXTURE_GOOD"]["validation_status"] == "PASS" and reg["FIXTURE_GOOD"]["latest_validation_run_id"] == r.run_id and reg["DOWNSIDE_DISLOCATION_RECOVERY"]["validation_status"] == "NOT_RUN"
    one = c.get(f"/api/strategy-registry/validation/runs/{r.run_id}").json()
    assert one["run_id"] == r.run_id and one["arms"]["RULES"]["trade_count"] == 100 and "trades" not in one["arms"]["RULES"]
    sims = c.get(f"/api/strategy-registry/validation/runs/{r.run_id}/simulations?arm=GATED").json()
    assert sims["count"] == 90 and "nothing was ever ordered" in sims["note"] and c.get(f"/api/strategy-registry/validation/runs/{r.run_id}/simulations?arm=X").status_code == 400
    au = c.get(f"/api/strategy-registry/validation/runs/{r.run_id}/audit").json()
    assert au["audit"]["status"] == "PASS" and au["integrity"]["same_candidate_set"] is True and len(au["candidates"]) == 100
    monkeypatch.delenv("STOCK_AGENT_ENV")
    assert c.get(f"/api/strategy-registry/validation/runs/{r.run_id}").status_code == 404 and c.get("/api/strategy-registry/FIXTURE_GOOD").json()["validation"]["validation_status"] == "NOT_RUN"


def test_R05_the_route_module_declares_get_routes_only_and_takes_no_body():
    for rel in ("api/routes/strategy_validation.py", "api/routes/strategy_registry.py"):
        src = (ROOT / rel).read_text(encoding="utf-8")
        decos = [ast.unparse(d.func) if isinstance(d, ast.Call) else ast.unparse(d) for n in ast.walk(ast.parse(src)) if isinstance(n, ast.FunctionDef) for d in n.decorator_list]
        assert decos and all(d == "router.get" for d in decos), (rel, decos)
        assert not re.search(r"router\.(post|put|patch|delete)|api_route|BaseModel|run_validation|subprocess", src), rel


# ================================================================ C CLI / replay / determinism =================================================

def test_C01_the_cli_validates_a_dataset_file_writes_the_run_and_persists_only_on_request(tmp_path, monkeypatch, capsys):
    from strategy_validation import cli
    from strategy_validation.store import StrategyValidationStore
    monkeypatch.setenv("STOCK_AGENT_ENV", "test")
    ds, ad = F.fixture("GOOD")
    dump(ds, str(tmp_path / "good.json"))
    assert load(str(tmp_path / "good.json")).dataset_id == ds.dataset_id and load(str(tmp_path / "good.json")).kind == "SYNTHETIC"
    out = tmp_path / "run.json"
    code = cli.main(["validate", "--strategy", "FIXTURE_GOOD", "--dataset", str(tmp_path / "good.json"), "--output", str(out), "--db", str(tmp_path / "v.db")], adapters={"FIXTURE_GOOD": ad})
    text = capsys.readouterr().out
    assert code == 0 and "FIXTURE_GOOD v1.0: PASS" in text and "OOS: 30 trades" in text and "200 / 200 trades validated" in text and "stored run" not in text
    j = json.loads(out.read_text(encoding="utf-8"))
    assert j["verdict"]["verdict"] == "PASS" and j["arms"]["MODEL"]["status"] == "NOT_EVALUATED" and StrategyValidationStore(tmp_path / "v.db").runs() == []
    assert cli.main(["validate", "--strategy", "FIXTURE_GOOD", "--dataset", str(tmp_path / "good.json"), "--persist", "--db", str(tmp_path / "v.db"),
                     "--neighbors", str(_write(tmp_path / "n.json", [{"keep": "mod:5:0,1,2,3,4"}]))], adapters={"FIXTURE_GOOD": ad}) == 0
    assert len(StrategyValidationStore(tmp_path / "v.db").runs("FIXTURE_GOOD")) == 1 and "ROBUST" in json.dumps(StrategyValidationStore(tmp_path / "v.db").latest("FIXTURE_GOOD")["diagnostics"]["sensitivity"])
    capsys.readouterr()
    assert cli.main(["runs", "--db", str(tmp_path / "v.db")]) == 0 and "FIXTURE_GOOD v1.0  PASS  SYNTHETIC  100 candidates" in capsys.readouterr().out
    assert cli.main(["validate", "--strategy", "FIXTURE_GOOD", "--dataset", str(tmp_path / "good.json"), "--arms", "rules,shorts"], adapters={"FIXTURE_GOOD": ad}) == 1
    assert cli.main(["validate", "--strategy", "FIXTURE_GOOD", "--dataset", str(tmp_path / "good.json"), "--train-ratio", "0.2"], adapters={"FIXTURE_GOOD": ad}) == 1
    assert cli.main(["validate", "--strategy", "FIXTURE_GOOD", "--dataset", str(tmp_path / "missing.csv")], adapters={"FIXTURE_GOOD": ad}) == 1


def _write(p, obj):
    p.write_text(json.dumps(obj), encoding="utf-8")
    return p


def test_C02_the_cli_reports_NOT_RUN_for_downside_recovery_and_the_placeholders_and_reads_a_csv(tmp_path, capsys):
    from strategy_validation import cli
    for sid in ("DOWNSIDE_DISLOCATION_RECOVERY", "NOT_A_STRATEGY"):                                        # Stage 6.4 - 6.7: every registry strategy but DDR has a replay
        assert cli.main(["validate", "--strategy", sid, "--dataset", "x.csv", "--output", str(tmp_path / f"{sid}.json")]) == 2
        assert json.loads((tmp_path / f"{sid}.json").read_text(encoding="utf-8"))["status"] == "NOT_RUN"
    out = capsys.readouterr().out
    assert "NOT_RUN DOWNSIDE_DISLOCATION_RECOVERY: No honest historical replay" in out and "NOT_RUN NOT_A_STRATEGY" in out
    csv = tmp_path / "bars.csv"
    csv.write_text("symbol,date,open,high,low,close,volume\naaa,2020-01-02,10,11,9,10.5,100\naaa,2020-01-03,10.5,11,10,10.8,100\n", encoding="utf-8")
    d = load(str(csv))
    assert d.kind == "HISTORICAL" and d.sessions == ["2020-01-02", "2020-01-03"] and d.dataset_id.startswith("sha256:")
    with pytest.raises(DatasetError):
        Dataset({"A": [{"date": "2020-01-02", "open": 10, "high": 9, "low": 8, "close": 10}]})       # high below open
    r = subprocess.run([sys.executable, "-m", "strategy_validation", "validate", "--strategy", "DOWNSIDE_DISLOCATION_RECOVERY", "--dataset", str(csv)], cwd=ROOT,
                       capture_output=True, text=True, env={**os.environ, "STOCK_AGENT_ENV": "test"})
    assert r.returncode == 2 and r.stdout.startswith("NOT_RUN DOWNSIDE_DISLOCATION_RECOVERY")


def test_C03_the_replay_foundation_compares_research_and_observed_candidate_states():
    r, _, _ = run("TOO_SMALL")
    research = RP.research_states(rec(r))
    assert len(research) == 10 and all(s.state == "CANDIDATE" and s.source == "RESEARCH" for s in research)
    same = RP.compare(research, [s.model_copy(update={"source": "PAPER", "state": "READY"}) for s in research])
    assert same["consistent"] is True and len(same["matched"]) == 10
    diff = RP.compare(research, [s.model_copy(update={"source": "PAPER", "state": "NO_CANDIDATE"}) for s in research[:9]])
    assert diff["consistent"] is False and len(diff["research_only"]) == 1 and len(diff["state_mismatch"]) == 9


def test_C04_a_run_is_deterministic_and_reproducible():
    a, _, _ = run("GOOD", gates=[F.SymbolGate()], model=F.DemandModel())
    b, _, _ = run("GOOD", gates=[F.SymbolGate()], model=F.DemandModel())
    assert a.run_id != b.run_id
    da, db = rec(a), rec(b)
    for k in ("run_id",):
        da.pop(k), db.pop(k)
    assert da == db
    assert a.validation_version == VERSION and a.disclaimer == DISCLAIMER and a.config_snapshot["adapter"]["strategy_version"] == "1.0" and a.friction_model == DEFAULT_FRICTION


# ================================================================ B broker-write boundary ======================================================

FORBIDDEN_IMPORTS = ("execution", "paper", "portfolio", "proposals", "safety", "account_refresh", "cash_flow", "trader_view", "alpaca", "robinhood", "broker", "requests", "httpx",
                     "urllib", "socket", "anthropic", "openai", "api.server", "scripts")


def _sources():
    files = sorted((ROOT / "strategy_validation").glob("*.py")) + [ROOT / "api" / "routes" / "strategy_validation.py", ROOT / "database" / "strategy_validation_migrations.py"]
    return {str(p.relative_to(ROOT)).replace("\\", "/"): p.read_text(encoding="utf-8") for p in files}


def test_B01_the_harness_imports_no_execution_broker_paper_or_network_code_and_calls_no_order_function():
    for rel, src in _sources().items():
        tree = ast.parse(src)
        mods = {n.module or "" for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)} | {a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
        bad = sorted(m for m in mods if any(m == f or m.startswith(f + ".") for f in FORBIDDEN_IMPORTS))
        assert bad == [], (rel, bad)
        calls = {ast.unparse(n.func).split(".")[-1] for n in ast.walk(tree) if isinstance(n, ast.Call)}
        assert not calls & {"submit_order", "place_order", "submit", "enable_auto", "set_mode", "run_cycle", "cancel_order", "post", "urlopen", "create_order"}, (rel, calls & {"submit", "post"})
        assert not re.search(r"(?i)alpaca|robinhood|PAPER_AUTO|ALPACA_API|/v2/orders", src), rel


def test_B02_a_validation_run_cannot_reach_a_broker_even_with_credentials_in_the_environment(tmp_path):
    ds, _ = F.fixture("GOOD")
    dump(ds, str(tmp_path / "good.json"))
    probe = ("import sys, json; sys.path.insert(0, sys.argv[1]); sys.path.insert(0, sys.argv[1] + '/tests')\n"
             "import socket\n"
             "def _no(*a, **k): raise RuntimeError('network refused in the boundary test')\n"
             "socket.socket.connect = _no; socket.create_connection = _no\n"
             "import strategy_validation_fixtures as F\n"
             "from strategy_validation import cli\n"
             "ds, ad = F.fixture('GOOD')\n"
             "code = cli.main(['validate', '--strategy', 'FIXTURE_GOOD', '--dataset', sys.argv[2], '--persist', '--db', sys.argv[3]], adapters={'FIXTURE_GOOD': ad})\n"
             "bad = sorted(m for m in sys.modules if m.split('.')[0] in ('execution', 'paper', 'portfolio', 'proposals', 'safety', 'alpaca_trade_api', 'alpaca', 'robin_stocks', 'requests', 'httpx', 'trader_view', 'account_refresh'))\n"
             "print(json.dumps({'code': code, 'bad': bad}))\n")
    env = {**os.environ, "STOCK_AGENT_ENV": "test", "ALPACA_API_KEY": "PKTEST000000", "ALPACA_SECRET_KEY": "x" * 40, "APCA_API_KEY_ID": "PKTEST000000",
           "APCA_API_SECRET_KEY": "x" * 40, "ROBINHOOD_API_KEY": "rh-test", "ROBINHOOD_PRIVATE_KEY": "rh-test", "EXECUTION_BROKER": "alpaca_paper"}
    r = subprocess.run([sys.executable, "-c", probe, str(ROOT), str(tmp_path / "good.json"), str(tmp_path / "v.db")], capture_output=True, text=True, env=env, cwd=str(tmp_path))
    out = json.loads(r.stdout.strip().splitlines()[-1])
    assert r.returncode == 0 and out == {"code": 0, "bad": []}, r.stdout[-2000:] + r.stderr[-2000:]


# ================================================================ N Strategy Lab display / CI =================================================

def test_N01_node_tests_of_the_validation_block():
    r = subprocess.run(["node", "--test", str(ROOT / "tests" / "frontend" / "strategy_validation.test.cjs")], capture_output=True, text=True, cwd=ROOT)
    assert r.returncode == 0 and re.search(r"pass 11\b", r.stdout) and re.search(r"fail 0\b", r.stdout), r.stdout[-3000:] + r.stderr[-2000:]


def test_N02_the_real_service_payloads_render_without_invented_numbers(tmp_path):
    from strategy_registry import service as SR
    payloads = {"none": SR.detail_view("DOWNSIDE_DISLOCATION_RECOVERY")["validation"]}
    for name in ("GOOD", "OVERFIT", "TOO_SMALL", "CANDIDATE_MISMATCH"):
        r, _, _ = run(name)
        payloads[name] = SV.detail(rec(r), r.strategy_id)
    (tmp_path / "p.json").write_text(json.dumps(payloads), encoding="utf-8")
    js = ("const P = require(process.argv[1]); const d = require(process.argv[2]); const t = (h) => h.replace(/<[^>]+>/g, ' ').replace(/&amp;/g, '&').replace(/\\s+/g, ' ');"
          "const out = {}; for (const k of Object.keys(d)) out[k] = t(P.validationHtml(d[k])); console.log(JSON.stringify(out));")
    r = subprocess.run(["node", "-e", js, str(ROOT / "frontend" / "product_ui.js"), str(tmp_path / "p.json")], capture_output=True, text=True, encoding="utf-8", cwd=ROOT)
    assert r.returncode == 0, r.stderr
    h = json.loads(r.stdout)
    assert "Not yet evaluated." in h["none"] and "No honest historical replay" in h["none"] and "Net P&L" not in h["none"] and "RULES arm" not in h["none"]
    assert "PASS · Eligible for review" in h["GOOD"] and "200 / 200 trades validated independently" in h["GOOD"] and "Not evaluated" in h["GOOD"] and "Synthetic test data" in h["GOOD"]
    assert "FAIL · Not ready for paper" in h["OVERFIT"] and h["OVERFIT"].count("This strategy should not progress to paper trading.") == 1
    assert "INSUFFICIENT DATA · Not ready for paper" in h["TOO_SMALL"] and "VALIDATION INVALID" in h["CANDIDATE_MISMATCH"] and "did not receive the same candidate set" in h["CANDIDATE_MISMATCH"]


def test_N03_ci_manifest_carries_this_file_and_every_file_is_grouped():
    import importlib.util
    groups = json.loads((ROOT / ".github" / "ci" / "pytest_groups.json").read_text(encoding="utf-8"))["groups"]
    assert "test_strategy_validation_62.py" in groups["research"]["files"]
    spec = importlib.util.spec_from_file_location("ci_pytest_groups", ROOT / "scripts" / "ci_pytest_groups.py")
    h = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(h)
    assert h.problems(h.load()) == []


# ================================================================ F freeze ========================================================================

def blob(rel: str) -> str:
    r = subprocess.run(["git", "show", f"{BASE}:{rel}"], cwd=ROOT, capture_output=True)
    assert r.returncode == 0, f"{rel} missing in {BASE} (fetch-depth 0 needed)"
    return r.stdout.replace(b"\r\n", b"\n").decode("utf-8")


def now_text(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8").replace("\r\n", "\n")


def tracked() -> set:
    return set(subprocess.run(["git", "ls-files", "--cached", "--others", "--exclude-standard"], cwd=ROOT, capture_output=True, text=True).stdout.split("\n")) - {""}


def test_F01_main_plus_only_the_stage_62_files_and_exact_edits():
    in_base = set(subprocess.run(["git", "ls-tree", "-r", "--name-only", BASE], cwd=ROOT, capture_output=True, text=True).stdout.split("\n")) - {""}
    assert in_base, "the base commit must be reachable (fetch-depth 0)"
    new = {p for p in tracked() - in_base if not p.startswith(("scratchpad/", "runtime/", "data/", "browser_tests/artifacts/")) and not p.endswith((".pyc", ".db"))}
    assert new == NEW_62, {"unexpected_new": sorted(new - NEW_62), "missing_new": sorted(NEW_62 - new)}
    assert all(p.endswith(".md") for p in NEW_63)                                                 # Stage 6.3 adds documentation only (no replay implementation)
    assert not any(p.startswith(("execution/", "safety/", "proposals/", "setups/", "strategy_registry/")) for p in NEW_631)   # Stage 6.3.1: the harness only
    assert {p.split("/")[0] for p in NEW_64} == {"value_quality", "strategy_registry", "tests", "DESIGN_64_VALUE_QUALITY.md"}   # Stage 6.4: the strategy package + its docs / tests
    changed = set(subprocess.run(["git", "diff", "--name-only", BASE], cwd=ROOT, capture_output=True, text=True).stdout.split("\n")) - {""} - new
    changed -= ledger_unchanged(changed, blob)                                                  # Stage 6.8: registered files equal to this base on main
    assert changed == CHANGED_62, {"unexpected_changes": sorted(changed - CHANGED_62), "missing_changes": sorted(CHANGED_62 - changed)}
    for rel in sorted(CHANGED_62):                                                              # each changed file: the base + its delimited block + its listed exact edits
        assert undo_62(rel, now_text(rel)) == blob(rel), f"{rel}: other than the Stage 6.2 block and the listed edits"
    assert not any(p.startswith(("execution/", "safety/", "proposals/", "setups/", "paper/", "portfolio/", "forward_validation/", "account_refresh/", "cash_flow/", "scripts/")) for p in (new - NEW_68 - NEW_691) | changed)   # Stage 6.8: its own files are pinned by tests/test_bot_activity_68.py; Stage 6.9.1's new execution file (an intentional semantic change) by tests/test_unified_exposure_gate_691.py
    for rel in ("api/server.py", "config.py", "database/database.py", "frontend/index.html", ".github/workflows/tests.yml", "strategy_registry/downside_recovery.py"):   # registry.py: Stage 6.4's two edits
        assert ledger_view(rel, now_text(rel)) == blob(rel), f"{rel} must not change"          # Stage 6.9: api/server.py / config.py registered in the ledger
    import forward_validation as FV
    import execution as X
    assert FV.frozen_hash() == FROZEN_HASH and X.MODES == ("MONITOR_ONLY", "PAPER_AUTO")       # the frozen controls and the execution modes: unchanged


def test_F02_one_marked_harness_block_that_writes_nothing_itself():
    src = now_text("browser_tests/flows.py")
    blocks = re.findall(STRIP_62["browser_tests/flows.py"], src)
    assert len(blocks) == 1 and src.count(MARK_62) == 1
    b = blocks[0]
    assert "def strategy_validation(c: Ctx):" in b and "def strategy_validation_results(c: Ctx):" in b and "c.js(TV_JS)" not in b and "_pf_boot(c)" in b
    assert 'c.http("POST"' not in b and ".post(" not in b and "enable_auto(" not in b and "run_cycle(" not in b and 'os.environ.get("STOCK_AGENT_ENV") == "browser_test"' in b
    assert "R.ENTRIES.remove(e)" in b and "finally:" in b                                        # the injected test-only entries are always removed
    for shot in ("strategy_validation_registry", "strategy_validation_none", "strategy_validation_registry_results", "strategy_validation_pass", "strategy_validation_insufficient", "strategy_validation_fail"):
        assert f'c.shot("{shot}"' in b, shot
    files = [p for p in tracked() if (ROOT / p).is_file() and not p.endswith((".pyc", ".db", ".png"))]
    assert sorted(p for p in files if MARK_62.encode("utf-8") in (ROOT / p).read_bytes()) == ["browser_tests/flows.py"]
    import browser_tests.flows as BF
    names = [f.__name__ for f in BF.FLOWS]
    assert names[names.index("scanner") + 1:names.index("scanner") + 4] == ["strategy_validation", "strategy_validation_results", "small_screens"]


# ================================================================ Z documents ======================================================================

def test_Z01_documents_state_the_method_and_the_limits():
    h, d, readme = now_text("STRATEGY_VALIDATION_HARNESS.md"), now_text("DESIGN_62_STRATEGY_VALIDATION.md"), now_text("README.md")
    for doc in (h, d):
        for s in ("PASS does not mean profitable in the future.", "PASS does not auto-promote a strategy.", "Backtest results are hypothetical."):
            assert s in doc, s
    for s in ("## 2. Candidate contract", "## 3. Three-arm methodology", "## 4. Out-of-sample (OOS)", "## 5. Friction and execution assumptions", "## 6. Metrics and the t-statistic",
              "## 7. Sample size", "## 8. Mechanical audit", "## 9. Verdict policy", "## 10. Parameter sensitivity and period concentration", "## 12. Limitations",
              "a **new `strategy_version`**", "STOP_FIRST", "MANUAL_CAUSALITY_REVIEW_REQUIRED", "NO_ADDITIONAL_GATES", "N / N trades validated", "Not yet evaluated"):
        assert s in h, s
    for s in ("Candidate Generation", "Immutable Candidate Set", "RULES / GATED / MODEL", "Mechanical Audit", "Human Lifecycle Review", "NOT: backtest passes → automatically trades"):
        assert s in d, s
    sec = re.findall(STRIP_62["README.md"], readme)
    assert len(sec) == 1 and "PASS only means eligible for human review" in sec[0] and 'Downside Dislocation Recovery reads "Not yet evaluated"' in sec[0]
    assert readme.index("## Unified product flow + Strategy Lab foundation (Stage 6.1.2)") < readme.index("## Strategy validation harness (Stage 6.2") < readme.index("## Continuous integration (Stage 6.0.3")
    assert not re.search(r"(?i)\b(guaranteed|profitable|buy now|sell now)\b", readme) and not re.search(r"(?i)\bregime\b", h + d + readme)


def test_Z02_stage_63_downside_recovery_replay_readiness_and_NOT_RUN_kept():
    doc = now_text("DESIGN_63_DDR_REPLAY_READINESS.md")
    for s in ("**Conclusion: FAITHFUL REPLAY NOT ESTABLISHED.**", "## 3. Strategy parity matrix", "| Live condition | Historical replay source | Replayable? | Parity | Notes |",
              "## 4. Missing inputs (concrete)", "## 5. Point-in-time provenance", "event_date", "data_available_at", "## 6. Dataset versioning", "## 7. Corporate actions",
              "## 8. Universe bias", "survivorship bias", "fixed, known symbol set", "Chosen policy: **PURGE**", "## 10. Persistence gate", "## 11. Candidate parity",
              "setups.engine.evaluate", "## 12. Forward-data collection plan", "## 13. Rejected shortcuts", "## 14. Definition of ready", "is NOT_RUN"):
        assert s in doc, s
    matrix = doc[doc.index("## 3. Strategy parity matrix"):doc.index("## 4. Missing inputs")]
    parity = re.findall(r"\| \*\*(EXACT|APPROXIMATE|UNAVAILABLE)\*\* \|", matrix)
    assert len(parity) >= 15 and "UNAVAILABLE" in parity and parity.count("EXACT") <= 1   # only TIME_EXIT is exact: no entry condition is
    for cond in ("PPE Confidence ≥ 40", "Evidence gate", "Market Demand ≥ 60"):
        row = next(l for l in matrix.splitlines() if l.startswith(f"| {cond}"))
        assert "**UNAVAILABLE**" in row, cond                                                  # the READY conditions have no point-in-time source
    from strategy_registry import registry as R
    from strategy_validation.adapters import ADAPTERS, DDR_REASON, DownsideRecoveryAdapter, adapter_for
    assert adapter_for("DOWNSIDE_DISLOCATION_RECOVERY", R.get("DOWNSIDE_DISLOCATION_RECOVERY")) == (None, DDR_REASON) and "DESIGN_63_DDR_REPLAY_READINESS.md" in DDR_REASON
    assert DownsideRecoveryAdapter.historical_replay_supported is False and sorted(ADAPTERS) == ["BREAKOUT", "DOWNSIDE_DISLOCATION_RECOVERY", "MEAN_REVERSION", "MOMENTUM", "VALUE_QUALITY"]   # no DDR replay adapter; Stage 6.4 - 6.7 add the four research strategies only
    from strategy_registry import service as SR
    assert SR.detail_view("DOWNSIDE_DISLOCATION_RECOVERY")["validation"]["validation_status"] == "NOT_RUN"
