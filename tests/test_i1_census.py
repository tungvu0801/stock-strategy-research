"""
I-1B-1 (S4): the static census of every route that can compute history (I1-C01..C11; Part C 1-2). The route table, the mains, the dynamic
imports, the import-path edits, the call sites, the strategy-core evaluate sites and the importer allow-lists are module-level literals here
(NF-8: no route table in research_gate/). The census reads source text and parses it; it never imports a scanned module.

This census is detection, not protection: it detects a committed route that evaluates without admission, but it does not stop an ad-hoc, notebook, agent-run or dynamically imported call, and it never runs at the moment of evaluation. MD-B is gated; the legacy paths are not yet.

Pinning form (RC-10): every pinned site is keyed by (path, enclosing scope qualname, ordinal of that call shape within the scope, stripped
source-line text), never by line number; the line numbers in the comments are citations only. Every assertion reports unexpected=... and
missing=... with the pinned key.
"""
import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SKIP = ("tests/", ".venv/", "scratchpad/", ".claude/")
PROTECTION_GATED = "RUNTIME: research_gate admission"
PROTECTION_NONE = ("NONE (detection only): a static census detects committed code; it does not stop an ad-hoc, notebook, agent-run or dynamically "
                   "imported call, and it never runs at evaluation time")
TRACKED_E5 = "ALLOWLISTED_TRACKED_FEEDS_E5"
SENTENCE = ("This census is detection, not protection: it detects a committed route that evaluates without admission, but it does not stop an ad-hoc, "
            "notebook, agent-run or dynamically imported call, and it never runs at the moment of evaluation. MD-B is gated; the legacy paths are not yet.")

ROUTES = (
    {"id": "E1", "route": "python -m strategy_validation validate", "entry": "strategy_validation/__main__.py:6 -> cli.py:37, :157-181", "computes_history": True,
     "label": "LEGACY_UNGATED_PENDING_I1B2", "closure": "I-1B-2 LG-1 (ID-4, deferred to the S5 gate by D-4)"},
    {"id": "E2", "route": "python -m strategy_validation evaluate", "entry": "strategy_validation/cli.py:50, :102-118; cross_sectional.py:538", "computes_history": True,
     "label": "LEGACY_UNGATED_PENDING_I1B2", "closure": "I-1B-2"},
    {"id": "E3", "route": "python -m swing_eval.evaluate", "entry": "swing_eval/evaluate.py:200, :217; PL.run :132", "computes_history": True,
     "label": "LEGACY_UNGATED_PENDING_I1B2", "closure": "I-1B-2"},
    {"id": "E4", "route": "swing_pullback.plan.run", "entry": "swing_pullback/plan.py:30-33", "computes_history": True, "label": "PENDING_ID11",
     "closure": "the ID-11 option, deferred to the S5 gate (D-4; none selected): census-only is not protection"},
    {"id": "E5", "route": "strategy_validation.engine.run_validation", "entry": "strategy_validation/engine.py:125", "computes_history": True,
     "label": "CENSUS_ONLY_PER_D5", "closure": "RR-1 (accepted for S4 only, D-3; not resolved)"},
    {"id": "E6", "route": "md_eval.engine.run", "entry": "md_eval/engine.py (run)", "computes_history": True, "label": "GATED_I1B1", "closure": "S4"},
    {"id": "E7", "route": "md_eval.signals sources and md_eval.benchmarks (md_eval.verdict is not gated: RR-2)", "entry": "md_eval/signals.py, md_eval/benchmarks.py",
     "computes_history": True, "label": "GATED_I1B1", "closure": "S4"},
    {"id": "E8", "route": "python -m research_data.quality", "entry": "research_data/quality.py:217, :239", "computes_history": False, "label": "INTEGRITY_ONLY",
     "closure": "-"},
    {"id": "E9", "route": "the browser harness flow (browser_tests/run.py)", "entry": "browser_tests/flows.py:3703-3744", "computes_history": True,
     "label": "HARNESS_SYNTHETIC", "closure": "I-1B-2 legacy pins"},
    {"id": "E9b", "route": "the harness world (Stage 3.2 backtests on a seeded synthetic market)", "entry": "browser_tests/world.py:60-131", "computes_history": True,
     "label": "HARNESS_SYNTHETIC_PENDING_OWNER", "closure": "AA-16 (accepted, D-7a)"},
    {"id": "E10", "route": "Stage 4.8-5.2 portfolio research routes", "entry": "api/routes/{portfolio_backtest,portfolio_walkforward,model_campaign,rotation_diagnostics,"
     "signal_research}.py; market_scan/flags.py:110-133", "computes_history": True, "label": "UNGATED_PRODUCT_RESEARCH", "closure": "ID-9 (an open HIGH risk; a record)"},
    {"id": "E11", "route": "POST /api/backtests, POST /api/backtests/preflight", "entry": "api/routes/backtests.py:66-67, :81-82", "computes_history": True,
     "label": "UNGATED_PRODUCT_RESEARCH", "closure": "ID-9"},
    {"id": "E12", "route": "python -m ppe.replay, python -m demand.replay (CLI only)", "entry": "ppe/replay.py:283-299, demand/replay.py:222-238",
     "computes_history": True, "label": "UNGATED_REPLAY_PENDING_OWNER",
     "closure": "an open HIGH risk tracked separately from ID-9 (D-5): a tracking label only, authorizing no ungated replay, no historical evaluation and no "
                "use of replay outputs for trading decisions"},
)
for _r in ROUTES:
    _r["protection"] = PROTECTION_GATED if _r["label"] == "GATED_I1B1" else PROTECTION_NONE
LABELS = {"E1": "LEGACY_UNGATED_PENDING_I1B2", "E2": "LEGACY_UNGATED_PENDING_I1B2", "E3": "LEGACY_UNGATED_PENDING_I1B2", "E4": "PENDING_ID11",
          "E5": "CENSUS_ONLY_PER_D5", "E6": "GATED_I1B1", "E7": "GATED_I1B1", "E8": "INTEGRITY_ONLY", "E9": "HARNESS_SYNTHETIC",
          "E9b": "HARNESS_SYNTHETIC_PENDING_OWNER", "E10": "UNGATED_PRODUCT_RESEARCH", "E11": "UNGATED_PRODUCT_RESEARCH", "E12": "UNGATED_REPLAY_PENDING_OWNER"}
CLASSIFICATIONS = {"LIBRARY_PART", "LIBRARY_PART_E10", "STORED_EVIDENCE_READ", "ACQUISITION", "NOT_EVALUATION", "CI_TOOLING", "MANIFEST_BUILD"}

MAINS = {
    ('strategy_validation/__main__.py', '<module>', 0, '<__main__.py>'): ('E1', 'E2', 'STORED_EVIDENCE_READ'),   # :0
    ('strategy_validation/__main__.py', '<module>', 0, 'if __name__ == "__main__":'): ('E1', 'E2', 'STORED_EVIDENCE_READ'),   # :6
    ('strategy_validation/cli.py', 'build_parser', 0, 'p = argparse.ArgumentParser(prog="python -m strategy_validation", description="Stage 6.2 strategy validation harness (research only; never orders)")'): ('E1', 'E2', 'STORED_EVIDENCE_READ'),   # :35
    ('swing_eval/evaluate.py', '<module>', 0, 'if __name__ == "__main__":'): ('E3',),   # :217
    ('swing_eval/evaluate.py', 'main', 0, 'ap = argparse.ArgumentParser(prog="python -m swing_eval.evaluate", description="the offline V1 / C1 evaluator")'): ('E3',),   # :200
    ('swing_eval/manifest.py', '<module>', 0, 'if __name__ == "__main__":'): ('MANIFEST_BUILD',),   # :258
    ('swing_eval/manifest.py', 'main', 0, 'ap = argparse.ArgumentParser(prog="python -m swing_eval.manifest", description="build an evaluation manifest (offline; for the operator\'s approval)")'): ('MANIFEST_BUILD',),   # :220
    ('research_data/quality.py', '<module>', 0, 'if __name__ == "__main__":'): ('E8',),   # :239
    ('research_data/quality.py', 'main', 0, 'ap = argparse.ArgumentParser(prog="python -m research_data.quality", description="the read-only pre-run data-quality report")'): ('E8',),   # :217
    ('browser_tests/run.py', '<module>', 0, 'if __name__ == "__main__":'): ('NOT_EVALUATION',),   # :111
    ('browser_tests/run.py', 'parse', 0, 'p = argparse.ArgumentParser(prog="python -m browser_tests.run", description="Stock Agent browser regression harness")'): ('NOT_EVALUATION',),   # :28
    ('research_data/sip_bundle.py', '<module>', 0, 'if __name__ == "__main__":'): ('ACQUISITION',),   # :346
    ('research_data/sip_bundle.py', 'main', 0, 'ap = argparse.ArgumentParser(prog="python -m research_data.sip_bundle", description=__doc__.split("\\n\\n")[0])'): ('ACQUISITION',),   # :307
    ('value_quality/ingest.py', '<module>', 0, 'if __name__ == "__main__":'): ('ACQUISITION',),   # :204
    ('value_quality/ingest.py', 'main', 0, 'p = argparse.ArgumentParser(prog="python -m value_quality.ingest", description="Stage 6.4 data ingestion (market data + SEC; never orders)")'): ('ACQUISITION',),   # :182
    ('market_intel/ingest.py', '<module>', 0, 'if __name__ == "__main__":'): ('ACQUISITION',),   # :569
    ('market_intel/ingest.py', 'main', 0, 'ap = argparse.ArgumentParser(prog="python -m market_intel.ingest", description="Stage 7.1 market-intelligence backfill planner (offline)")'): ('ACQUISITION',),   # :518
    ('market_intel/sources/cli.py', '<module>', 0, 'if __name__ == "__main__":'): ('ACQUISITION',),   # :131
    ('market_intel/sources/cli.py', 'main', 0, 'ap = argparse.ArgumentParser(prog="python -m market_intel.sources.cli", description="Stage 7.2 adapters (offline)")'): ('ACQUISITION',),   # :92
    ('market_intel/schema_v2.py', '<module>', 0, 'if __name__ == "__main__":'): ('NOT_EVALUATION',),   # :526
    ('market_intel/schema_v2.py', 'main', 0, 'ap = argparse.ArgumentParser(prog="python -m market_intel.schema_v2", description="Stage 7.3.1 schema v2: status and the explicit copy migration")'): ('NOT_EVALUATION',),   # :501
    ('market_intel/quality.py', '<module>', 0, 'if __name__ == "__main__":'): ('NOT_EVALUATION',),   # :1266
    ('market_intel/quality.py', 'main', 0, 'ap = argparse.ArgumentParser(prog="python -m market_intel.quality", description="Stage 7.4 data quality and latency report (offline, read-only)",'): ('NOT_EVALUATION',),   # :1233
    ('market_scan/run.py', '<module>', 0, 'if __name__ == "__main__":'): ('NOT_EVALUATION',),   # :623
    ('market_scan/run.py', 'main', 0, 'ap = argparse.ArgumentParser(prog="python -m market_scan.run", description="Stage 7.5 shared market scanner (offline, read-only, research only)",'): ('NOT_EVALUATION',),   # :594
    ('main.py', '<module>', 0, 'if __name__ == "__main__":'): ('NOT_EVALUATION',),   # :300
    ('main.py', 'main', 0, 'parser = argparse.ArgumentParser('): ('NOT_EVALUATION',),   # :264
    ('execution/recovery.py', '<module>', 0, 'if __name__ == "__main__":'): ('NOT_EVALUATION',),   # :442
    ('execution/recovery.py', 'main', 0, 'p = argparse.ArgumentParser(prog="python -m execution.recovery", description="Bot-position recovery after a manual exit on Alpaca\'s website (database rows only)")'): ('NOT_EVALUATION',),   # :412
    ('forward_validation/run_admin.py', '<module>', 0, 'if __name__ == "__main__":'): ('NOT_EVALUATION',),   # :330
    ('forward_validation/run_admin.py', 'main', 0, 'p = argparse.ArgumentParser(prog="python -m forward_validation.run_admin", description="Wave 2 PR C: the forward run (start / end / status)")'): ('NOT_EVALUATION',),   # :288
    ('tools/evaluator_capacity.py', '<module>', 0, 'if __name__ == "__main__":'): ('NOT_EVALUATION',),   # :666
    ('tools/evaluator_capacity.py', 'main', 0, 'ap = argparse.ArgumentParser(prog="python -B -m tools.evaluator_capacity", description=__doc__.strip().splitlines()[0])'): ('NOT_EVALUATION',),   # :606
    ('tools/verify_exit_fills.py', '<module>', 0, 'if __name__ == "__main__":'): ('NOT_EVALUATION',),   # :111
    ('tools/verify_exit_fills.py', 'main', 0, 'ap = argparse.ArgumentParser(description="Read-only check of historical exit-fill accounting (run on a byte copy).")'): ('NOT_EVALUATION',),   # :102
    ('scripts/run_stock_agent.py', '<module>', 0, 'if __name__ == "__main__":'): ('NOT_EVALUATION',),   # :423
    ('scripts/run_stock_agent.py', 'parse', 0, 'ap = argparse.ArgumentParser(description="Stock Agent background server process (started by scripts/start_stock_agent.ps1).")'): ('NOT_EVALUATION',),   # :266
    ('scripts/run_aapl_paper_smoke.py', '<module>', 0, 'if __name__ == "__main__":'): ('NOT_EVALUATION',),   # :712
    ('scripts/run_aapl_paper_smoke.py', 'parse', 0, 'ap = argparse.ArgumentParser(description="Stage 6.0 supervised one-share AAPL Alpaca PAPER smoke (standalone).")'): ('NOT_EVALUATION',),   # :635
    ('scripts/ci_pytest_groups.py', '<module>', 0, 'if __name__ == "__main__":'): ('CI_TOOLING',),   # :122
    ('ppe/replay.py', '<module>', 0, 'if __name__ == "__main__":'): ('E12',),   # :299
    ('ppe/replay.py', 'main', 0, 'p = argparse.ArgumentParser(description="Point-in-time PPE replay over the stored daily bars (read only).")'): ('E12',),   # :284
    ('demand/replay.py', '<module>', 0, 'if __name__ == "__main__":'): ('E12',),   # :238
    ('demand/replay.py', 'main', 0, 'p = argparse.ArgumentParser(prog="python -m demand.replay", description=__doc__.strip().splitlines()[0])'): ('E12',),   # :223
}
DYNAMIC_IMPORTS = {
    ('browser_tests/flows.py', 'strategy_validation_results', 0, 'spec = importlib.util.spec_from_file_location("sv_fixtures_harness", Path(__file__).resolve().parents[1] / "tests" / "strategy_validation_fixtures.py")'): {"target": "tests/strategy_validation_fixtures.py", "disposition": "E9 (allow-listed, I-1A)", "tracking": TRACKED_E5},   # :3720
    ('browser_tests/flows.py', 'strategy_validation_results', 1, 'vspec = importlib.util.spec_from_file_location("vq_fixtures_harness", Path(__file__).resolve().parents[1] / "tests" / "value_quality_fixtures.py")'): {"target": "tests/value_quality_fixtures.py", "disposition": "E9 (census recognition only, AA-18)", "tracking": TRACKED_E5},   # :3730
    ('browser_tests/flows.py', 'strategy_validation_results', 0, 'FX = importlib.util.module_from_spec(spec)'): {"target": "the spec of :3720", "disposition": "E9", "tracking": None},   # :3721
    ('browser_tests/flows.py', 'strategy_validation_results', 1, 'VF = importlib.util.module_from_spec(vspec)'): {"target": "the spec of :3730", "disposition": "E9", "tracking": None},   # :3731
    ('browser_tests/flows.py', 'strategy_validation_results', 0, 'spec.loader.exec_module(FX)'): {"target": "the spec of :3720", "disposition": "E9", "tracking": None},   # :3722
    ('browser_tests/flows.py', 'strategy_validation_results', 1, 'vspec.loader.exec_module(VF)'): {"target": "the spec of :3730", "disposition": "E9", "tracking": None},   # :3732
    ('autorun/service.py', 'stop_components', 0, '__import__(mod, fromlist=["shutdown"]).shutdown()'): {"target": "mod in ('forward_validation.scheduler', 'execution.scheduler') (:186)", "disposition": "NOT_EVALUATION (lease-loss shutdown)", "tracking": None},   # :188
    ('api/routes/evidence.py', 'packet', 0, 'if not __import__("re").match(_ID, packet_id):'): {"target": "re (stdlib)", "disposition": "allow-listed stdlib literal", "tracking": None},   # :120
    ('api/routes/portfolio_backtest.py', 'get_run', 0, '"bars": __import__("json").loads(run["bars_json"])})'): {"target": "json (stdlib)", "disposition": "allow-listed stdlib literal (an E10 route module)", "tracking": None},   # :133
    ('forward_validation/service.py', 'ForwardValidationService.reconciliation', 0, 'healthy_runs += 1 if _n(__import__("json").loads(r["summary_json"] or "{}").get("findings")) == 0 else 0'): {"target": "json (stdlib)", "disposition": "allow-listed stdlib literal", "tracking": None},   # :1143
    ('forward_validation/service.py', 'ForwardValidationService.safety_fires', 0, 'e["reason_codes"] = __import__("json").loads(e.pop("reason_codes_json") or "[]")'): {"target": "json (stdlib)", "disposition": "allow-listed stdlib literal", "tracking": None},   # :1184
    ('forward_validation/service.py', 'ForwardValidationService.broker_health', 0, 'bump("BROKER_UNAVAILABLE:" + str(__import__("json").loads(r["detail_json"] or "{}").get("code")))'): {"target": "json (stdlib)", "disposition": "allow-listed stdlib literal", "tracking": None},   # :1256
    ('forward_validation/service.py', 'ForwardValidationService._trade', 0, 'inputs = __import__("json").loads(ready[0]["inputs_json"] or "{}")'): {"target": "json (stdlib)", "disposition": "allow-listed stdlib literal", "tracking": None},   # :1367
    ('forward_validation/service.py', 'ForwardValidationService.trace', 0, 'tr["inputs"] = __import__("json").loads(tr.pop("inputs_json") or "{}")'): {"target": "json (stdlib)", "disposition": "allow-listed stdlib literal", "tracking": None},   # :1402
    ('historical_data/calendar.py', '_find_tzdata', 0, 'return importlib.util.find_spec("tzdata")'): {"target": "tzdata (spec lookup, no execution)", "disposition": "allow-listed non-executing", "tracking": None},   # :48
    ('research_gate/admit.py', '_tzdata_dirs', 0, 'spec = importlib.util.find_spec("tzdata")'): {"target": "tzdata (spec lookup, no execution)", "disposition": "allow-listed non-executing (PC-10)", "tracking": None},   # :83
}
CALL_SITES = {
    ('strategy_validation/cli.py', 'main', 0, 'run = E.run_validation(adapter, dataset, train_ratio=args.train_ratio, arms=arms, gates=(), model=None, neighbours=neighbours,'): "run_validation (E1)",   # :181
    ('swing_pullback/plan.py', 'run', 0, 'return E.run_validation(adapter(version), dataset, arms=("RULES",), friction=friction(cost), created_at=created_at)'): "run_validation (E4 -> E5)",   # :33
    ('browser_tests/flows.py', 'strategy_validation_results', 0, 'runs[name] = E.run_validation(ad, ds, gates=[FX.SymbolGate()] if name == "GOOD" else ())'): "run_validation (E9)",   # :3742
    ('browser_tests/flows.py', 'strategy_validation_results', 1, 'csr = E.run_validation(cs_adapter({"top_n": 5}), VF.dataset(VF.run_set(), end="2022-12-30"))   # Stage 6.4.1: one cross-sectional run'): "run_validation (E9)",   # :3744
    ('strategy_validation/cli.py', '_assess_universe', 0, 'a = UNI.assess_stored_run(run, getattr(adapter, "universe_declaration", None), store.latest_evaluation(args.run), git_sha=E.git_commit_sha())'): "assess_stored_run (universe assessment, STORED_EVIDENCE_READ)",   # :80
    ('strategy_validation/cli.py', '_evaluate', 0, 'ev = CS.assess_stored_run(run, store.candidates(args.run), store.trades(args.run, "RULES"), dataset, adapter, registry=R.by_id(), git_sha=E.git_commit_sha())'): "assess_stored_run (E2)",   # :118
    ('swing_eval/evaluate.py', 'evaluate', 0, 'recs[(r["version"], r["cost"])] = json.loads(PL.run(r["version"], r["cost"], ds, created_at=created).model_dump_json())'): "plan.run (E3 -> E4)",   # :132
}
CORE_EVALUATE_SITES = {
    ('breakout/diagnostics.py', '_period_returns', 0, 'decs = [core.evaluate(R, universe, data) for R in core.schedule(dataset.sessions)]'): "LIBRARY_PART",   # :105
    ('breakout/diagnostics.py', 'momentum_overlap', 0, 'mom = mc.evaluate(r["rebalance"], universe, data)'): "LIBRARY_PART",   # :202
    ('breakout/adapter.py', 'BreakoutAdapter.decisions', 0, 'dec = self.core.evaluate(R, universe, data, prev=prev)              # selection is stateless; prev only labels enter / hold / exit'): "LIBRARY_PART",   # :97
    ('mean_reversion/diagnostics.py', '_period_returns', 0, 'decs = [core.evaluate(R, universe, data) for R in core.schedule(dataset.sessions)]'): "LIBRARY_PART",   # :92
    ('mean_reversion/diagnostics.py', 'momentum_overlap', 0, 'mom = mc.evaluate(r["rebalance"], universe, data)'): "LIBRARY_PART",   # :151
    ('mean_reversion/adapter.py', 'MeanReversionAdapter.decisions', 0, 'dec = self.core.evaluate(R, universe, data, prev=prev)              # selection is stateless; prev only labels enter / hold / exit'): "LIBRARY_PART",   # :92
    ('momentum/diagnostics.py', '_period_returns', 0, 'decs = [core.evaluate(R, universe, data) for R in core.schedule(dataset.sessions)]'): "LIBRARY_PART",   # :58
    ('momentum/adapter.py', 'MomentumAdapter.decisions', 0, 'dec = self.core.evaluate(R, universe, data, prev=prev)              # selection is stateless; prev only labels enter / hold / exit'): "LIBRARY_PART",   # :85
    ('value_quality/adapter.py', 'ValueQualityAdapter.candidates', 0, 'dec = self.core.evaluate(R, universe, data, data.calendar, prev=prev)   # selection is stateless; prev only labels enter / hold / exit'): "LIBRARY_PART",   # :106
    ('swing_pullback/adapter.py', 'SwingTrendPullbackAdapter.candidates', 0, 'ev = self.core.evaluate(dataset)'): "LIBRARY_PART",   # :79
    ('md_eval/signals.py', 'SwingBridge.entries', 0, 'for sg in self.core.evaluate(ds)["signals"]:'): "E7",   # :177
}
SYS_PATH_EDITS = {
    ('browser_tests/run.py', 'run', 0, 'sys.path.insert(0, p)'): "the harness: puts tests/ on sys.path",   # :39
    ('browser_tests/app_server.py', 'prepare', 0, 'sys.path.insert(0, p)'): "the harness server: puts tests/ on sys.path",   # :112
    ('scripts/run_stock_agent.py', '_run', 0, 'sys.path.insert(0, str(app_root))'): "the app root",   # :315
    ('scripts/run_aapl_paper_smoke.py', 'main', 0, 'sys.path.insert(0, str(app_root))'): "the app root",   # :669
    ('scripts/ci_pytest_groups.py', 'main', 0, 'sys.path.insert(0, str(ROOT))'): "the repository root",   # :115
    ('tools/evaluator_capacity.py', 'imports_blocked', 0, 'sys.meta_path.insert(0, gate)'): "sys.meta_path gate (insert)",   # :355
    ('tools/evaluator_capacity.py', 'imports_blocked', 1, 'sys.meta_path.remove(gate)'): "sys.meta_path gate (remove)",   # :360
}
HARNESS_TEST_IMPORTS = {("browser_tests/world.py", "bt_fixtures"), ("browser_tests/world.py", "fw_fixtures"), ("browser_tests/app_server.py", "pf_fixtures"),
                        ("browser_tests/app_server.py", "test_explain_38"), ("browser_tests/app_server.py", "alpaca_order_fakes"),
                        ("browser_tests/flows.py", "alpaca_order_fakes"), ("browser_tests/flows.py", "pf_fixtures")}
IMPORTERS = {   # (watched module, how matched, the package whose own files are excluded): the expected non-test importers in the S4 tree
    ("strategy_validation.engine", "exact", "strategy_validation/"): {"strategy_validation/cli.py", "swing_pullback/plan.py", "swing_pullback/diagnostics.py",
                                                                    "swing_eval/codetree.py", "browser_tests/flows.py"},
    ("swing_pullback.plan", "exact", "swing_pullback/"): {"swing_eval/evaluate.py", "swing_eval/manifest.py"},
    ("swing_eval", "prefix", "swing_eval/"): set(),
    ("md_eval.engine", "exact", "md_eval/"): set(),
    ("md_eval.signals", "exact", "md_eval/"): set(),
    ("md_eval.benchmarks", "exact", "md_eval/"): set(),
    ("md_eval.verdict", "exact", "md_eval/"): set(),
    ("research_gate", "prefix", "research_gate/"): {"md_eval/engine.py", "md_eval/signals.py", "md_eval/benchmarks.py", "md_eval/verdict.py"},
    ("historical_data", "prefix", "historical_data/"): {"research_gate/admit.py", "research_gate/bridge.py", "research_gate/snapshot.py"},
    ("strategy_validation.cross_sectional", "exact", "strategy_validation/"): {"breakout/diagnostics.py", "mean_reversion/diagnostics.py", "momentum/diagnostics.py"},
    ("rotation.engine", "exact", "rotation/"): {"rotation_backtest/simulator.py", "rotation_backtest/runner.py", "signal_research/variant_engine.py",
                                                "api/routes/portfolio_rotation.py"},
    ("ppe.replay", "exact", "ppe/"): {"demand/replay.py"},
}
STRATEGY_CORE_PACKAGES = ("breakout", "mean_reversion", "momentum", "value_quality", "swing_pullback")
STRATEGY_CORE_IMPORTS = {("breakout/diagnostics.py", "momentum.core"), ("mean_reversion/diagnostics.py", "momentum.core"), ("browser_tests/flows.py", "value_quality.adapter"),
                         ("md_eval/signals.py", "swing_pullback.core"), ("swing_eval/analysis.py", "swing_pullback.diagnostics"), ("swing_eval/evaluate.py", "swing_pullback.adapter"),
                         ("strategy_validation/adapters.py", "value_quality.adapter"), ("strategy_validation/adapters.py", "momentum.adapter"),
                         ("strategy_validation/adapters.py", "mean_reversion.adapter"), ("strategy_validation/adapters.py", "breakout.adapter")}
CONSTRUCTOR_SITES = {   # (path, enclosing scope, constructor) outside the defining packages and tests/
    ("research_gate/admit.py", "admit_from_source.<locals>.body", "Ms1EvidenceProvider"), ("research_gate/admit.py", "admit_from_source.<locals>.body", "SipDailyV1Provider"),
    ("research_gate/snapshot.py", "snapshot_double", "ContractDouble"), ("research_gate/admit.py", "_issue", "Admission"), ("research_gate/admit.py", "_issue", "AdmittedDataset"),
    ("research_gate/bridge.py", "_issue_view", "AdmittedView"),
}
BACKTEST_IMPORTERS = {
    "comparison/view.py": "NON_EVALUATION", "forward/store.py": "NON_EVALUATION", "forward/capture.py": "NON_EVALUATION", "forward/journal.py": "NON_EVALUATION",
    "forward/automation.py": "NON_EVALUATION", "fit/readonly.py": "NON_EVALUATION", "fit/evidence.py": "NON_EVALUATION", "fit/scanner.py": "NON_EVALUATION",
    "fit/current.py": "NON_EVALUATION", "paper/portfolio.py": "NON_EVALUATION", "paper/execution.py": "NON_EVALUATION", "paper/alpaca_orders.py": "NON_EVALUATION",
    "api/routes/forward_tests.py": "NON_EVALUATION", "rotation/engine.py": "LIBRARY_PART_E10", "browser_tests/app_server.py": "NON_EVALUATION",
    "browser_tests/world.py": "HARNESS_SYNTHETIC_PENDING_OWNER", "market_scan/flags.py": "HISTORICAL_COMPUTE_E10", "api/routes/backtests.py": "E11_ROUTE",
    "rotation_backtest/calendar.py": "HISTORICAL_COMPUTE_E10", "rotation_backtest/simulator.py": "HISTORICAL_COMPUTE_E10", "rotation_backtest/runner.py": "HISTORICAL_COMPUTE_E10",
    "rotation_walkforward/engine.py": "HISTORICAL_COMPUTE_E10", "rotation_walkforward/regimes.py": "HISTORICAL_COMPUTE_E10", "rotation_campaign/engine.py": "HISTORICAL_COMPUTE_E10",
    "rotation_diagnostics/engine.py": "HISTORICAL_COMPUTE_E10", "rotation_diagnostics/attribution.py": "HISTORICAL_COMPUTE_E10", "signal_research/engine.py": "HISTORICAL_COMPUTE_E10",
    "signal_research/regime.py": "HISTORICAL_COMPUTE_E10", "signal_research/variant_engine.py": "HISTORICAL_COMPUTE_E10",
}
BACKTEST_CLASSES = {"NON_EVALUATION", "LIBRARY_PART_E10", "HISTORICAL_COMPUTE_E10", "E11_ROUTE", "HARNESS_SYNTHETIC_PENDING_OWNER"}
E10_PACKAGES = ("rotation_backtest", "rotation_walkforward", "rotation_campaign", "rotation_diagnostics", "signal_research")
E10_ROUTES = ("api/routes/portfolio_backtest.py", "api/routes/portfolio_walkforward.py", "api/routes/model_campaign.py", "api/routes/rotation_diagnostics.py",
              "api/routes/signal_research.py")
E10_OUTSIDE_IMPORTERS = {("md_eval/metrics.py", "rotation_backtest.metrics"), ("market_scan/flags.py", "rotation_walkforward.regimes")}
ADAPTER_KEYS = {"DOWNSIDE_DISLOCATION_RECOVERY", "VALUE_QUALITY", "MOMENTUM", "MEAN_REVERSION", "BREAKOUT"}
FORBIDDEN_FOR_GATE = ("backtest", "rotation_walkforward", "rotation_campaign", "rotation_diagnostics", "signal_research", "rotation_backtest")
DYNAMIC_CALLEES = {"import_module", "__import__", "spec_from_file_location", "module_from_spec", "exec_module", "SourceFileLoader", "run_module", "run_path", "find_spec"}


# ============================================================================================================ the static scan
def _files():
    out = {}
    for p in sorted(ROOT.rglob("*.py")):
        rel = p.relative_to(ROOT).as_posix()
        if rel.startswith(SKIP) or "/site-packages/" in rel:
            continue
        out[rel] = p
    return out


def _module_of(rel):
    parts = rel[:-3].split("/")
    return ".".join(parts[:-1] if parts[-1] == "__init__" else parts)


def _is_module(name):
    base = ROOT.joinpath(*name.split("."))
    return base.with_suffix(".py").is_file() or (base / "__init__.py").is_file()


class _Scan(ast.NodeVisitor):
    """Every node with its enclosing scope qualname (def / class nesting; comprehensions and lambdas are not scopes here)."""

    def __init__(self):
        self.stack, self.items = [], []

    def _scope(self):
        return self.stack[-1] if self.stack else "<module>"

    def _enter(self, node, kind):
        parent = self.stack[-1] if self.stack else None
        name = node.name if parent is None else (f"{parent[0]}.<locals>.{node.name}" if parent[1] == "def" else f"{parent[0]}.{node.name}")
        self.stack.append((name, kind))
        self.generic_visit(node)
        self.stack.pop()

    def visit_FunctionDef(self, node):
        self.items.append((node, self.stack[-1][0] if self.stack else "<module>"))
        self._enter(node, "def")

    visit_AsyncFunctionDef = visit_FunctionDef

    def visit_ClassDef(self, node):
        self.items.append((node, self.stack[-1][0] if self.stack else "<module>"))
        self._enter(node, "class")

    def generic_visit(self, node):
        for child in ast.iter_child_nodes(node):
            if not isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                self.items.append((child, self.stack[-1][0] if self.stack else "<module>"))
            self.visit(child)


_CACHE = {}


def _parsed():
    if not _CACHE:
        for rel, p in _files().items():
            text = p.read_text(encoding="utf-8", errors="replace")
            tree = ast.parse(text)
            scan = _Scan()
            scan.visit(tree)
            _CACHE[rel] = (tree, text.split("\n"), scan.items)
    return _CACHE


def _callee(call):
    f = call.func
    if isinstance(f, ast.Attribute):
        return f.attr
    if isinstance(f, ast.Name):
        return f.id
    return None


def _keyed(found):
    """[(rel, scope, shape, node, lines)] -> {(rel, scope, ordinal, stripped line): lineno}, ordinals per (rel, scope, shape) in source order."""
    out, count = {}, {}
    for rel, scope, shape, node, lines in sorted(found, key=lambda x: (x[0], x[3].lineno, x[3].col_offset)):
        k = (rel, scope, shape)
        n = count.get(k, 0)
        count[k] = n + 1
        out[(rel, scope, n, lines[node.lineno - 1].strip())] = node.lineno
    return out


def _calls(pred):
    found = []
    for rel, (tree, lines, items) in _parsed().items():
        for node, scope in items:
            if isinstance(node, ast.Call):
                shape = pred(rel, node)
                if shape:
                    found.append((rel, scope, shape, node, lines))
    return _keyed(found)


def _edges(per_statement=False):
    """{rel: {imported module names}} at any depth; relative imports resolved; `from X import y` also names X.y when it is a module.
    per_statement=True: one citation per statement - `from X import y` names only X.y when y is a module (else X), as the §1.6 E10 rows cite."""
    out = {}
    for rel, (tree, _, items) in _parsed().items():
        pkg = _module_of(rel) if rel.endswith("__init__.py") else _module_of(rel).rpartition(".")[0]
        mods = set()
        for node, _ in items:
            if isinstance(node, ast.Import):
                mods |= {a.name for a in node.names}
            elif isinstance(node, ast.ImportFrom):
                base = node.module or ""
                if node.level:
                    up = pkg.split(".")[:len(pkg.split(".")) - (node.level - 1)] if pkg else []
                    base = ".".join([p for p in up if p] + ([node.module] if node.module else []))
                subs = {f"{base}.{a.name}" for a in node.names if _is_module(f"{base}.{a.name}")}
                if not (per_statement and subs):
                    mods.add(base)
                mods |= subs
        out[rel] = mods
    return out


def _importers(watched, how, own):
    hit = lambda m: m == watched or (how == "prefix" and m.startswith(watched + "."))  # noqa: E731
    return {rel for rel, mods in _edges().items() if not rel.startswith(own) and any(hit(m) for m in mods)}


def _diff(found, pinned):
    return {"unexpected": sorted(set(found) - set(pinned)), "missing": sorted(set(pinned) - set(found))}


LEGACY_ROOTS = ("run_validation", "assess_stored_run")
_ALIASES = {}


def _import_aliases(tree, rel=""):
    """{local name: {fully qualified targets}} for every import statement at any depth (D-01/D-02; Plan C 1.2 `:211`, AA-9 (b)(2)).
    `import a.b as X` -> X: {"a.b"}; `from M import n as X` -> X: {"M.n"}; `from M import n` -> n: {"M.n"}; relative imports are resolved as in _edges.
    A plain `import a.b` binds `a` (no entry; _dotted resolves the chain). A name bound twice keeps every target (over-detection, never under-detection)."""
    pkg = ""
    if rel.endswith(".py"):
        pkg = _module_of(rel) if rel.endswith("__init__.py") else _module_of(rel).rpartition(".")[0]
    out = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                if a.asname:
                    out.setdefault(a.asname, set()).add(a.name)
        elif isinstance(node, ast.ImportFrom):
            base = node.module or ""
            if node.level:
                up = pkg.split(".")[:len(pkg.split(".")) - (node.level - 1)] if pkg else []
                base = ".".join([p for p in up if p] + ([node.module] if node.module else []))
            for a in node.names:
                if a.name != "*":
                    out.setdefault(a.asname or a.name, set()).add(f"{base}.{a.name}" if base else a.name)
    return out


def _aliases(rel):
    if rel not in _ALIASES:
        _ALIASES[rel] = _import_aliases(_parsed()[rel][0], rel)
    return _ALIASES[rel]


def _resolved_names(name, aliases):
    """The local name plus the original (last-component) name of every import it is bound to: a superset, so no literal match is ever lost."""
    return {name} | {t.rpartition(".")[2] for t in aliases.get(name, ())}


def _dotted(expr, aliases):
    """Every fully qualified dotted name an attribute chain can denote, with its root name resolved through the import aliases."""
    parts = []
    while isinstance(expr, ast.Attribute):
        parts.append(expr.attr)
        expr = expr.value
    if not isinstance(expr, ast.Name):
        return set()
    tail = ".".join(reversed(parts))
    return {r + ("." + tail if tail else "") for r in aliases.get(expr.id, set()) | {expr.id}}


def _c03_shape(call, aliases):
    """The C03 call shape: "run_validation" / "assess_stored_run" / "plan.run", or None. Shapes are the ORIGINAL names, so CALL_SITES keys never change."""
    f = call.func
    if isinstance(f, ast.Attribute) and f.attr in LEGACY_ROOTS:                                         # unchanged: E.run_validation(...)
        return f.attr
    if isinstance(f, ast.Name):
        hit = sorted(_resolved_names(f.id, aliases) & set(LEGACY_ROOTS))                                # [site A1] run_validation(...) or an alias of it
        if hit:
            return hit[0]
        if "swing_pullback.plan.run" in aliases.get(f.id, ()):                                          # [site A2] from swing_pullback.plan import run as r; r(...)
            return "plan.run"
        return None
    if isinstance(f, ast.Attribute) and f.attr == "run":
        if isinstance(f.value, ast.Name) and f.value.id in ("PL", "plan"):                               # unchanged literal receivers
            return "plan.run"
        if "swing_pullback.plan" in _dotted(f.value, aliases):                                          # [site A2] P.run / sp.run / swing_pullback.plan.run
            return "plan.run"
    return None


def _constructor_name(call, aliases, names):
    """The original constructor name when the call builds one of `names` (attribute, bare name or an import alias of it), else None."""
    f = call.func
    if isinstance(f, ast.Attribute):
        return f.attr if f.attr in names else None
    if isinstance(f, ast.Name):
        hit = sorted(_resolved_names(f.id, aliases) & set(names))                                        # [site A3]
        return hit[0] if hit else None
    return None


def _mem(src, rel="mem/probe.py"):
    """Parse an in-memory contract-named form (never written to the scanned tree)."""
    tree = ast.parse(src)
    return tree, _import_aliases(tree, rel)


# ============================================================================================================ the tests
def _mains():
    found = []
    for rel, (tree, lines, items) in _parsed().items():
        if rel.endswith("/__main__.py") or rel == "__main__.py":
            found.append((rel, "<module>", "main_file", ast.Pass(lineno=0, col_offset=0), ["<__main__.py>"] + lines))
        argparse_names = {a.asname or a.name for n, _ in items if isinstance(n, ast.ImportFrom) and n.module == "argparse" for a in n.names if a.name == "ArgumentParser"}
        for node in tree.body:
            if isinstance(node, ast.If) and isinstance(node.test, ast.Compare) and isinstance(node.test.left, ast.Name) and node.test.left.id == "__name__" \
                    and len(node.test.ops) == 1 and isinstance(node.test.ops[0], ast.Eq) and isinstance(node.test.comparators[0], ast.Constant) \
                    and node.test.comparators[0].value == "__main__":
                found.append((rel, "<module>", "main_guard", node, lines))
        for node, scope in items:
            if isinstance(node, ast.Call) and ((isinstance(node.func, ast.Attribute) and node.func.attr == "ArgumentParser" and isinstance(node.func.value, ast.Name)
                                                and node.func.value.id == "argparse") or (isinstance(node.func, ast.Name) and node.func.id in argparse_names)):
                found.append((rel, scope, "argparse", node, lines))
    out, count = {}, {}
    for rel, scope, shape, node, lines in sorted(found, key=lambda x: (x[0], x[3].lineno)):
        n = count.get((rel, scope, shape), 0)
        count[(rel, scope, shape)] = n + 1
        text = "<__main__.py>" if shape == "main_file" else lines[node.lineno - 1].strip()
        out[(rel, scope, n, text)] = node.lineno
    return out


def test_C01_every_main_is_in_the_route_table():
    found = _mains()
    assert set(found) == set(MAINS), _diff(found, MAINS)
    names = {r["id"] for r in ROUTES} | CLASSIFICATIONS
    assert all(set(v) <= names for v in MAINS.values()), [k for k, v in MAINS.items() if not set(v) <= names]


def _rebound_builtins(items):
    return {t.id for n, _ in items if isinstance(n, (ast.Assign, ast.AnnAssign)) for t in (n.targets if isinstance(n, ast.Assign) else [n.target])
            if isinstance(t, ast.Name)} | {n.name for n, _ in items if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}


def test_C02_every_dynamic_import_and_import_path_edit_is_listed():
    rebound = {rel: _rebound_builtins(items) for rel, (_, _, items) in _parsed().items()}

    def dyn(rel, call):
        c = _callee(call)
        if c in DYNAMIC_CALLEES:
            return c
        if isinstance(call.func, ast.Name) and call.func.id in ("exec", "eval", "compile") and call.func.id not in rebound[rel]:
            return call.func.id
        return None
    found = _calls(dyn)
    assert set(found) == set(DYNAMIC_IMPORTS), _diff(found, DYNAMIC_IMPORTS)
    tracked = {k for k, v in DYNAMIC_IMPORTS.items() if v["tracking"] is not None}
    assert {(k[0], k[3].split(" = ")[0]) for k in tracked} == {("browser_tests/flows.py", "spec"), ("browser_tests/flows.py", "vspec")}
    assert all(v["tracking"] == TRACKED_E5 for k, v in DYNAMIC_IMPORTS.items() if k in tracked)

    def edit(rel, call):
        f = call.func
        if isinstance(f, ast.Attribute) and f.attr in ("insert", "append", "extend", "remove", "pop", "clear") and isinstance(f.value, ast.Attribute) \
                and isinstance(f.value.value, ast.Name) and f.value.value.id == "sys" and f.value.attr in ("path", "meta_path"):
            return "sys_path_edit"
        return None
    edits = _calls(edit)
    for rel, (tree, lines, items) in _parsed().items():
        for node, _ in items:
            if isinstance(node, (ast.Assign, ast.AugAssign)):
                for t in (node.targets if isinstance(node, ast.Assign) else [node.target]):
                    t = t.value if isinstance(t, ast.Subscript) else t
                    assert not (isinstance(t, ast.Attribute) and isinstance(t.value, ast.Name) and t.value.id == "sys" and t.attr in ("path", "meta_path")), \
                        (rel, node.lineno)
    assert set(edits) == set(SYS_PATH_EDITS), _diff(edits, SYS_PATH_EDITS)
    harness = set()
    for rel, (tree, _, items) in _parsed().items():
        if rel.startswith("browser_tests/"):
            for node, _ in items:
                names = [a.name for a in node.names] if isinstance(node, ast.Import) else ([node.module] if isinstance(node, ast.ImportFrom) and node.module and not node.level else [])
                harness |= {(rel, n.split(".")[0]) for n in names if (ROOT / "tests" / (n.split(".")[0] + ".py")).is_file()}
    assert harness == HARNESS_TEST_IMPORTS, _diff(harness, HARNESS_TEST_IMPORTS)
    pycache_py = {rel for rel in _files() if "/__pycache__/" in "/" + rel}
    assert pycache_py == set(), sorted(pycache_py)


def test_C03_legacy_roots_have_only_their_listed_callers():
    found = _calls(lambda rel, call: _c03_shape(call, _aliases(rel)))
    assert set(found) == set(CALL_SITES), _diff(found, CALL_SITES)
    for key in (("strategy_validation.engine", "exact", "strategy_validation/"), ("swing_pullback.plan", "exact", "swing_pullback/"), ("swing_eval", "prefix", "swing_eval/")):
        got = _importers(*key) | ({"strategy_validation/cli.py"} if key[0] == "strategy_validation.engine" and "strategy_validation/cli.py" in
                                  {r for r, m in _edges().items() if key[0] in m} else set())
        assert got == IMPORTERS[key], (key, _diff(got, IMPORTERS[key]))
    pairs = set()
    for rel, mods in _edges().items():
        for pkg in STRATEGY_CORE_PACKAGES:
            if not rel.startswith(pkg + "/"):
                pairs |= {(rel, m) for m in mods if m in (f"{pkg}.core", f"{pkg}.adapter", f"{pkg}.diagnostics")}
    assert pairs == STRATEGY_CORE_IMPORTS, _diff(pairs, STRATEGY_CORE_IMPORTS)
    key = ("strategy_validation.cross_sectional", "exact", "strategy_validation/")
    assert _importers(*key) == IMPORTERS[key]
    core_files = tuple(p + "/" for p in STRATEGY_CORE_PACKAGES)
    ev = _calls(lambda rel, c: "evaluate" if isinstance(c.func, ast.Attribute) and c.func.attr == "evaluate" and (rel.startswith(core_files) or rel == "md_eval/signals.py") else None)
    assert set(ev) == set(CORE_EVALUATE_SITES), _diff(ev, CORE_EVALUATE_SITES)


def test_C03a_aliased_legacy_root_calls_are_detected():
    SRC = ("from strategy_validation.engine import run_validation as rv\n"
           "from strategy_validation.cross_sectional import assess_stored_run as asr\n"
           "import strategy_validation.engine as E\n"
           "def f(a, d, r):\n"
           "    rv(a, d)\n"
           "    asr(r)\n"
           "    E.run_validation(a, d)\n"
           "    run_validation(a, d)\n"
           "    rvx(a)\n")
    tree, al = _mem(SRC)
    got = sorted((c.lineno, _c03_shape(c, al)) for c in ast.walk(tree) if isinstance(c, ast.Call))
    assert got == [(5, "run_validation"), (6, "assess_stored_run"), (7, "run_validation"), (8, "run_validation"), (9, None)], got


def test_C03b_plan_run_through_aliased_or_dotted_receivers_is_detected():
    SRC = ("import swing_pullback.plan as P\n"
           "from swing_pullback import plan as sp\n"
           "import swing_pullback.plan\n"
           "from swing_pullback.plan import run as prun\n"
           "import other.plan as Q\n"
           "import swing_pullback as SP\n"
           "from . import plan as rp\n"
           "from .plan import run\n"
           "def g(self, v, c, ds):\n"
           "    P.run(v, c, ds)\n"
           "    sp.run(v, c, ds)\n"
           "    swing_pullback.plan.run(v, c, ds)\n"
           "    prun(v, c, ds)\n"
           "    PL.run(v, c, ds)\n"
           "    Q.run(v, c, ds)\n"
           "    self.plan.run(v, c, ds)\n"
           "    SP.plan.run(v, c, ds)\n"
           "    rp.run(v, c, ds)\n"
           "    run(v, c, ds)\n")
    tree, al = _mem(SRC, "swing_pullback/probe.py")
    got = sorted((c.lineno, _c03_shape(c, al)) for c in ast.walk(tree) if isinstance(c, ast.Call))
    assert got == [(10, "plan.run"), (11, "plan.run"), (12, "plan.run"), (13, "plan.run"), (14, "plan.run"), (15, None), (16, None),
                   (17, "plan.run"), (18, "plan.run"), (19, "plan.run")], got


def test_C03c_aliases_reads_the_real_tree_per_file_and_caches():
    # S5-M2-L3: _aliases(rel) on the real tree (C03a/C03b/C05a/C07a parse in-memory sources through _mem): md_eval/engine.py:21, :22, :30
    rel = "md_eval/engine.py"
    al = _aliases(rel)
    assert {k: al.get(k) for k in ("BK", "PR", "RGATE")} == {"BK": {"md_eval.book"}, "PR": {"md_eval.prereg"}, "RGATE": {"research_gate"}}, al
    assert _aliases(rel) is al and _ALIASES[rel] is al                                                  # cached per rel
    other = _aliases("cash_flow/reader.py")                                                             # the same local name in another real file (:23)
    assert other.get("BK") == {"execution.broker"}, other


def test_C04_md_eval_entry_modules_and_research_gate_have_only_their_listed_importers():
    for key in (("md_eval.engine", "exact", "md_eval/"), ("md_eval.signals", "exact", "md_eval/"), ("md_eval.benchmarks", "exact", "md_eval/"),
                ("md_eval.verdict", "exact", "md_eval/"), ("research_gate", "prefix", "research_gate/")):
        got = _importers(*key)
        assert got == IMPORTERS[key], (key, _diff(got, IMPORTERS[key]))


def _constructor_sites(names):
    out = set()
    for rel, (tree, _, items) in _parsed().items():
        for node, scope in items:
            name = _constructor_name(node, _aliases(rel), names) if isinstance(node, ast.Call) else None
            if name:
                out.add((rel, scope, name))
    return out


def test_C05_md_a_is_named_only_by_research_gate():
    key = ("historical_data", "prefix", "historical_data/")
    assert _importers(*key) == IMPORTERS[key], _diff(_importers(*key), IMPORTERS[key])
    got = {s for s in _constructor_sites({"SyntheticProvider", "Ms1EvidenceProvider", "SipDailyV1Provider"}) if not s[0].startswith("historical_data/")}
    want = {s for s in CONSTRUCTOR_SITES if s[2] in ("SyntheticProvider", "Ms1EvidenceProvider", "SipDailyV1Provider")}
    assert got == want, _diff(got, want)


def test_C05a_aliased_md_a_provider_constructors_are_detected():
    NAMES = {"SyntheticProvider", "Ms1EvidenceProvider", "SipDailyV1Provider"}
    SRC = ("from historical_data.adapters.synthetic import SyntheticProvider as SP\n"
           "from historical_data.adapters.ms1_evidence import Ms1EvidenceProvider as MP\n"
           "from historical_data.adapters.sip_daily_v1 import SipDailyV1Provider as DP\n"
           "import historical_data.adapters.synthetic as HS\n"
           "def h():\n"
           "    SP(1)\n"
           "    MP(2)\n"
           "    DP(3)\n"
           "    HS.SyntheticProvider(4)\n"
           "    Other(5)\n")
    tree, al = _mem(SRC)
    got = sorted((c.lineno, _constructor_name(c, al, NAMES)) for c in ast.walk(tree) if isinstance(c, ast.Call))
    assert got == [(6, "SyntheticProvider"), (7, "Ms1EvidenceProvider"), (8, "SipDailyV1Provider"), (9, "SyntheticProvider"), (10, None)], got


def test_C06_the_strategy_validation_adapter_keys_are_listed():
    tree = _parsed()["strategy_validation/adapters.py"][0]
    keys = None
    for node in tree.body:
        targets = node.targets if isinstance(node, ast.Assign) else ([node.target] if isinstance(node, ast.AnnAssign) else [])
        if any(isinstance(t, ast.Name) and t.id == "ADAPTERS" for t in targets):
            keys = {k.value for k in node.value.keys}
    assert keys == ADAPTER_KEYS


def test_C07_admission_objects_are_built_only_by_research_gate():
    got = _constructor_sites({"Admission", "AdmittedDataset", "AdmittedView", "ContractDouble"})
    got = {s for s in got if not (s[2] == "ContractDouble" and s[0].startswith("md_eval/"))}
    want = {s for s in CONSTRUCTOR_SITES if s[2] in ("Admission", "AdmittedDataset", "AdmittedView", "ContractDouble")}
    assert got == want, _diff(got, want)


def test_C07a_aliased_admission_constructors_are_detected():
    NAMES = {"Admission", "AdmittedDataset", "AdmittedView", "ContractDouble"}
    SRC = ("from research_gate.admit import Admission as A, AdmittedDataset as AD\n"
           "from research_gate.bridge import AdmittedView as V\n"
           "from research_gate.snapshot import ContractDouble as CD\n"
           "def k():\n"
           "    A()\n"
           "    AD()\n"
           "    V()\n"
           "    CD()\n"
           "    Admission()\n"
           "    Unrelated()\n")
    tree, al = _mem(SRC)
    got = sorted((c.lineno, _constructor_name(c, al, NAMES)) for c in ast.walk(tree) if isinstance(c, ast.Call))
    assert got == [(5, "Admission"), (6, "AdmittedDataset"), (7, "AdmittedView"), (8, "ContractDouble"), (9, "Admission"), (10, None)], got


def test_C08_no_md_eval_subpackage_bypasses_X02():
    assert sorted((ROOT / "md_eval").rglob("*.py")) == sorted((ROOT / "md_eval").glob("*.py")) and len(list((ROOT / "md_eval").glob("*.py"))) == 18
    trv = ast.parse((ROOT / "tests" / "test_md_eval_registry_verdict.py").read_text(encoding="utf-8"))
    allowed = next(tuple(e.value for e in n.value.elts) for n in trv.body if isinstance(n, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "ALLOWED" for t in n.targets))
    for rel, mods in _edges().items():
        if rel.startswith("md_eval/"):
            assert all(any(m == q or m.startswith(q + ".") for q in allowed) for m in mods), (rel, sorted(mods))
        if rel.startswith("research_gate/"):
            assert not mods & {"md_eval.engine", "md_eval.signals", "md_eval.benchmarks", "md_eval.verdict"}, rel


def test_C09_backtest_E10_rotation_and_replay_importers_are_classified():
    got = _importers("backtest", "prefix", "backtest/")
    assert got == set(BACKTEST_IMPORTERS), _diff(got, BACKTEST_IMPORTERS)
    assert set(BACKTEST_IMPORTERS.values()) <= BACKTEST_CLASSES and len(BACKTEST_IMPORTERS) == 29
    family = tuple(p + "/" for p in E10_PACKAGES)
    outside = {(rel, m) for rel, mods in _edges(per_statement=True).items() if not rel.startswith(family) and rel not in E10_ROUTES
               for m in mods if any(m == p or m.startswith(p + ".") for p in E10_PACKAGES)}
    assert outside == E10_OUTSIDE_IMPORTERS, _diff(outside, E10_OUTSIDE_IMPORTERS)
    for key in (("rotation.engine", "exact", "rotation/"), ("ppe.replay", "exact", "ppe/")):
        assert _importers(*key) == IMPORTERS[key], (key, _diff(_importers(*key), IMPORTERS[key]))


def test_C10_labels_are_exactly_the_decided_ones_and_detection_is_not_protection():
    assert {r["id"]: r["label"] for r in ROUTES} == LABELS
    assert {r["label"] for r in ROUTES} == set(LABELS.values())
    for r in ROUTES:
        assert r["protection"] == (PROTECTION_GATED if r["label"] == "GATED_I1B1" else PROTECTION_NONE), r["id"]
    assert {r["id"] for r in ROUTES if "PENDING" in r["label"]} == {"E1", "E2", "E3", "E4", "E9b", "E12"}
    assert SENTENCE in " ".join(__doc__.split())


def test_C11_md_eval_and_research_gate_import_no_E10_or_E11_entry():
    for rel, mods in _edges().items():
        if rel.startswith(("md_eval/", "research_gate/")):
            bad = {m for m in mods if any(m == p or m.startswith(p + ".") for p in FORBIDDEN_FOR_GATE) and m != "rotation_backtest.metrics"}
            assert not bad, (rel, sorted(bad))
