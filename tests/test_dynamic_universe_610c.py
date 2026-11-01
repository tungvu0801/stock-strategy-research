"""
Stage 6.10C - the INACTIVE dynamic universe (dynamic_universe/; DESIGN_610C_DECISION_FLOW.md section 8) and the offline capacity tool.

U  settings, the frozen core v1, the core-file override, the future selection rule, the status / plan views, the declared-only signals
I  isolation: no live module imports the package, the evaluator / Trader View / watchlist files are byte-identical to the base, and the new
   flags move neither the forward-capture cohort id nor its config hash
T  the capacity tool runs offline (sockets are blocked by conftest) and writes nothing outside its work directory
"""
import json
import re
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

import config  # noqa: E402
import dynamic_universe as DU  # noqa: E402
from dynamic_universe import core as CORE  # noqa: E402
from dynamic_universe import plan as PLAN  # noqa: E402
from dynamic_universe import signals as SIG  # noqa: E402

BASE_610C = "fed5b3cd3d602eccd71e473046ce58ad2f7819f4"
LIVE = ["NVDA", "AMD", "MU", "SNDK", "AVGO", "MRVL", "LLY", "ORCL", "SHOP", "CLS"]          # watchlist.txt as the evaluator reads it
SYMBOL = re.compile(r"^[A-Z][A-Z.\-]{0,9}$")


def cfg(monkeypatch, **kw):
    for k, v in kw.items():
        monkeypatch.setattr(config, k, v)


def used_names(src: str):
    """What the code actually uses: imported modules, names and attributes (a seam named in a docstring or a label is not a use of it)."""
    import ast
    names = set()
    for n in ast.walk(ast.parse(src)):
        if isinstance(n, ast.Import):
            names.update(a.name for a in n.names)
        elif isinstance(n, ast.ImportFrom):
            names.add(n.module or "")
            names.update(a.name for a in n.names)
        elif isinstance(n, ast.Name):
            names.add(n.id)
        elif isinstance(n, ast.Attribute):
            names.add(n.attr)
    return names


# ================================================================ U settings ===============================================================
def test_U01_off_is_the_default_and_every_other_mode_but_shadow_reads_as_off_and_is_reported(monkeypatch):
    assert config.DYNAMIC_UNIVERSE_MODE == "OFF" and config.DYNAMIC_UNIVERSE_CORE_FILE == "" and config.DYNAMIC_UNIVERSE_ACTIVE_MAX == 25
    s = PLAN.settings()
    assert (s["mode"], s["configured_mode"], s["active_max"], s["problems"]) == ("OFF", "OFF", 25, [])
    cfg(monkeypatch, DYNAMIC_UNIVERSE_MODE="ACTIVE")
    s = PLAN.settings()
    assert s["mode"] == "OFF" and s["configured_mode"] == "ACTIVE" and len(s["problems"]) == 1 and "later stage" in s["problems"][0]
    cfg(monkeypatch, DYNAMIC_UNIVERSE_MODE="banana")
    s = PLAN.settings()
    assert s["mode"] == "OFF" and "BANANA" in s["problems"][0]
    cfg(monkeypatch, DYNAMIC_UNIVERSE_MODE="shadow")
    assert PLAN.settings()["mode"] == "SHADOW" and PLAN.settings()["problems"] == []
    assert DU.MODES == ("OFF", "SHADOW") and "ACTIVE" not in DU.MODES


def test_U02_the_active_bound_is_normalised_never_fatal(monkeypatch):
    for bad in (0, -1, 501, "x", True, None, 2.5):
        cfg(monkeypatch, DYNAMIC_UNIVERSE_ACTIVE_MAX=bad)
        s = PLAN.settings()
        assert s["active_max"] == 25 and any("DYNAMIC_UNIVERSE_ACTIVE_MAX" in p for p in s["problems"]), bad
    cfg(monkeypatch, DYNAMIC_UNIVERSE_ACTIVE_MAX="40")
    assert PLAN.settings()["active_max"] == 40


# ================================================================ U the core universe v1 ===================================================
def test_U03_core_v1_is_frozen_pinned_and_holds_every_live_symbol():
    core = CORE.builtin()
    assert core.version == "core_v1" and core.count == len(CORE.CORE_V1) == 79 and core.problems == ()
    assert core.sha256 == CORE.CORE_V1_SHA256 == CORE.digest(CORE.CORE_V1)
    syms = core.symbols
    assert len(set(syms)) == len(syms) and all(SYMBOL.match(s) for s in syms)
    assert set(LIVE) <= set(syms) and "GOOG" not in syms and "GOOGL" in syms              # one share class; the three the sector map lacks are in
    sectors = core.sectors()
    assert sectors["Unmapped"] == 2 and {"CLS", "SHOP"} == {s for s, sec in core.entries if sec == "Unmapped"}
    assert ("SNDK", "Semiconductors") in core.entries                                     # config.EVIDENCE_SECTOR_EXTENSIONS places it; never guessed
    from data.sector_map import SECTOR_MAP
    mapped = {p for info in SECTOR_MAP.values() for p in info["peers"]} - {"GOOG"}
    assert set(syms) == mapped | {"CLS", "SHOP", "SNDK"}
    used = used_names((ROOT / "dynamic_universe" / "core.py").read_text(encoding="utf-8"))
    assert "SECTOR_MAP" not in used and not any("sector_map" in u for u in used)         # a literal: editing the sector map cannot move it


def test_U04_a_core_file_overrides_with_every_bad_entry_dropped_and_reported_and_never_an_empty_core(tmp_path, monkeypatch):
    p = tmp_path / "core.json"
    p.write_text(json.dumps([{"symbol": "aapl", "sector": "Technology"}, {"symbol": "MSFT"}, {"symbol": "AAPL", "sector": "dup"}, {"symbol": "BAD TICKER"},
                             {"symbol": 7}, {"symbol": "XOM", "sector": 3}, "junk"]), encoding="utf-8")
    core = CORE.load_core(p)
    assert core.entries == (("AAPL", "Technology"), ("MSFT", "Unmapped"), ("XOM", "Unmapped")) and core.sha256 == CORE.digest(core.entries)
    assert core.version != "core_v1" or core.source != CORE.BUILTIN_SOURCE
    assert len(core.problems) == 5 and any("repeats" in x for x in core.problems) and any("not a valid ticker" in x for x in core.problems)
    cfg(monkeypatch, DYNAMIC_UNIVERSE_CORE_FILE=str(p))
    assert CORE.load_core().entries == core.entries                                        # the config path is honoured
    for text, why in (("{not json", "unreadable"), ('{"a": 1}', "expected a JSON list"), ("[]", "no valid entry"), ('[{"symbol": "??"}]', "no valid entry")):
        p.write_text(text, encoding="utf-8")
        c = CORE.load_core(p)
        assert c.entries == CORE.CORE_V1 and c.sha256 == CORE.CORE_V1_SHA256 and any(why in x for x in c.problems), (text, c.problems)
    missing = CORE.load_core(tmp_path / "nope.json")
    assert missing.entries == CORE.CORE_V1 and any("not found" in x for x in missing.problems)
    p.write_text(json.dumps([{"symbol": f"S{i}" if i else "SA"} for i in range(0)] + [{"symbol": "A" + chr(65 + i % 26) + chr(65 + i // 26)} for i in range(520)]), encoding="utf-8")
    capped = CORE.load_core(p)
    assert capped.count == CORE.CORE_FILE_MAX == 500 and any("beyond the cap" in x for x in capped.problems)
    big = tmp_path / "big.json"
    big.write_bytes(b"[" + b" " * CORE.CORE_FILE_MAX_BYTES + b"]")
    assert CORE.load_core(big).entries == CORE.CORE_V1


# ================================================================ U the future selection rule ==============================================
def test_U05_select_active_keeps_live_and_held_symbols_bounds_only_the_candidates_and_is_deterministic():
    core = ["AAPL", "MSFT", "XOM", "JPM", "NVDA", "AMD"]
    out = PLAN.select_active(core, ["XOM", "MSFT", "AAPL"], ["NVDA", "AMD"], ["TSLA"], 4)
    assert out == ("NVDA", "AMD", "TSLA", "XOM")                                           # live, then kept (outside the core too), then candidates
    assert PLAN.select_active(core, ["XOM", "MSFT", "AAPL"], ["NVDA", "AMD"], ["TSLA"], 4) == out
    assert PLAN.select_active(core, ["XOM"], ["NVDA", "AMD", "MU"], ["TSLA", "LLY"], 2) == ("NVDA", "AMD", "MU", "TSLA", "LLY")   # never dropped by the bound
    assert PLAN.select_active(core, ["ZZZZ", "XOM"], [], [], 10) == ("XOM",)                # a candidate outside the core is never selected
    assert PLAN.select_active(core, [], ["nvda", "NVDA"], [], 10) == ("NVDA",)
    with pytest.raises(ValueError):
        PLAN.select_active(core, [], [], [], -1)
    assert SIG.implemented() == () and all(not s.implemented for s in SIG.SIGNALS) and len(SIG.SIGNALS) == 6
    assert {s.id for s in SIG.SIGNALS} == {"relative_volume", "gap_pct", "intraday_move", "sector_strength", "catalyst_flag", "unusual_volatility"}
    with pytest.raises(NotImplementedError):
        SIG.compute("NVDA")


def test_U06_status_and_plan_report_the_active_set_as_exactly_the_live_watchlist_in_off_and_shadow(monkeypatch):
    for mode in ("OFF", "SHADOW"):
        cfg(monkeypatch, DYNAMIC_UNIVERSE_MODE=mode)
        st = PLAN.status(LIVE + ["bad symbol"])
        assert st["mode"] == mode and st["live"]["symbols"] == LIVE and st["active"]["symbols"] == LIVE and st["active"]["equals_live"] is True
        assert st["core"]["count"] == 79 and st["core"]["live_in_core"] == 10 and st["scanner"]["implemented"] == 0 and st["scanner"]["candidates"] == []
        assert any("not a valid ticker" in p for p in st["problems"]) and "Inactive" in st["note"]
        pl = PLAN.plan(LIVE, keep_symbols=["CLS", "ABC"])
        assert pl["active"]["symbols"] == LIVE and pl["active"]["equals_live"] is True
        blob = json.dumps(pl)
        assert "core" in blob and "scanner" in blob and "evaluator" in blob.lower() and "risk" in blob.lower() and "execution" in blob.lower()
    with pytest.raises(AssertionError):
        PLAN._require_active_is_live(("NVDA",), ("NVDA", "AMD"))


# ================================================================ I isolation ==============================================================
def test_I01_no_live_module_imports_the_dynamic_universe_and_the_live_coverage_files_are_byte_identical_to_the_base():
    allowed = ("dynamic_universe/", "tools/", "tests/", "decision_flow/", "api/routes/decision_flow.py", "scratchpad/",
               "market_scan/")                                       # Stage 7.5: reads core.py, never activates
    offenders = []
    for p in ROOT.rglob("*.py"):
        rel = p.relative_to(ROOT).as_posix()
        if rel.startswith(allowed) or "/.venv/" in rel or rel.startswith(".venv/") or "__pycache__" in rel:
            continue
        if re.search(r"^\s*(from|import)\s+dynamic_universe\b", p.read_text(encoding="utf-8", errors="replace"), re.M):
            offenders.append(rel)
    assert offenders == []
    for rel in ("setups/service.py", "setups/scheduler.py", "trader_view/stream.py", "scanner/watchlist.py", "execution/service.py", "execution/engine.py",
                "watchlist.txt"):
        base = subprocess.run(["git", "show", f"{BASE_610C}:{rel}"], cwd=ROOT, capture_output=True).stdout.decode("utf-8").replace("\r\n", "\n")
        assert base and (ROOT / rel).read_text(encoding="utf-8").replace("\r\n", "\n") == base, rel
    used = set()
    for f in ("__init__.py", "core.py", "plan.py", "signals.py"):
        used |= used_names((ROOT / "dynamic_universe" / f).read_text(encoding="utf-8"))
    assert not used & {"symbols_in_scope", "evaluate_all", "watchlist_fn", "add_to_watchlist", "remove_from_watchlist", "requests", "urllib", "get_data_client",
                       "TradingClient", "robin_stocks", "post_order", "enable_auto", "set_mode", "load_watchlist", "subprocess"}   # no live seam is touched


def test_I02_the_new_flags_move_neither_the_cohort_id_nor_the_config_hash(monkeypatch):
    from forward_capture import cohort as CO
    import inspect
    fn = CO.snapshot if hasattr(CO, "snapshot") else None
    assert fn is not None
    params = inspect.signature(fn).parameters
    assert not any(n.startswith("DYNAMIC_UNIVERSE") for n in CO.__dict__), "the flags are not cohort inputs"
    text = inspect.getsource(CO)
    assert "DYNAMIC_UNIVERSE" not in text                                                   # neither COHORT_KEYS, FLAGS nor CADENCES name them
    assert len(params) >= 1


# ================================================================ T the capacity tool ======================================================
def test_T01_the_capacity_tool_measures_offline_and_writes_nothing_outside_its_work_directory(tmp_path):
    from tools import evaluator_capacity as CAP
    before = {p for p in ROOT.rglob("*") if p.is_file() and ".venv" not in p.parts and "__pycache__" not in p.parts and p.stat().st_mtime > 0}
    marks = {p: p.stat().st_mtime_ns for p in before if not p.name.endswith((".pyc",))}
    rep = CAP.measure(3, latency_ms=0.0, cycles=2, work_dir=tmp_path)
    s, g = rep["summary"], rep["guards"]
    assert rep["ok"] is True and s["n"] == 3 and s["healthy"] is True and s["errors_total"] == 0
    assert g["network_attempts"] == [] and g["wire_hits"] == [] and g["blocked_imports"] == [] and g["api_server_imported"] is False
    assert "FakeMarketDataClient" in g["market_data_client"] and "temporary" in g["database"]
    assert s["warm"]["requests_total_median"] > 0 and s["warm"]["wall_s_median"] > 0 and s["cold"]["requests_total"] > s["warm"]["requests_total_median"]
    assert s["requests_per_min_warm"] > 0 and set(s["warm"]["requests_by_kind_mean"]) <= {"snapshot", "minute_bars", "daily_bars_60d", "daily_bars_120d", "daily_bars_420d"}
    model = CAP.request_budget(100, latency_ms=130)
    assert model["requests_per_cycle"] >= 250 and model["cycle_s"] > 30 and model["total_requests_per_min"] > model["requests_per_cycle"] * 0.9
    assert CAP.request_budget(200, latency_ms=130)["over_interval"] is True and CAP.request_budget(10, latency_ms=130)["over_interval"] is False
    after = {p: p.stat().st_mtime_ns for p in marks if p.exists()}
    assert after == marks                                                                  # no repository file touched
    assert set(marks) == {p for p in marks if p.exists()}
