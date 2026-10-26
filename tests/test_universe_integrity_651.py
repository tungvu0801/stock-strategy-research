"""Stage 6.5.1 — survivorship / point-in-time universe integrity (strategy_validation/universe.py, DESIGN_651_UNIVERSE_INTEGRITY.md).

  A-C  classification: a fixed current universe is UNTRUSTED; a supplied point-in-time membership is TRUSTED; unknown / missing is never trusted
  D-E  the cap: a raw ESTABLISHED edge on a fixed universe is never a final ESTABLISHED; absolute performance and the raw edge stay intact
  F-G  Value + Quality and Momentum history: append-only assessments, the runs byte-identical, no outcome improves
  H-J  a missing declaration blocks persistence; event-driven strategies are unaffected; a fair control does not clear survivorship
  K-M  the point-in-time contract: no future membership, delisted / acquired securities until their removal, one identity across ticker changes
  N-Z  isolation, the gate, the store, the CLI, the Strategy Lab payload, pre-registration, documents

Fixtures are SYNTHETIC; test_F02 / G02 use the git-ignored local runtime data only where it exists (on a copy - the local database is never written)."""
from __future__ import annotations

import ast
import copy
import functools
import io
import json
import re
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

import momentum_fixtures as MF  # noqa: E402
import strategy_validation_fixtures as FX  # noqa: E402
import value_quality_fixtures as VF  # noqa: E402
from momentum.adapter import MomentumAdapter  # noqa: E402
from strategy_registry import registry as R  # noqa: E402
from strategy_validation import (EDGE_LADDER, SNAPSHOT_MAX_STALENESS_DAYS, UNIVERSE_EDGE_CAP, UNIVERSE_METHODS, UNIVERSE_POLICY_VERSION,  # noqa: E402
                                 UNIVERSE_INTEGRITY_STATUSES)
from strategy_validation import cross_sectional as CS  # noqa: E402
from strategy_validation import engine as E  # noqa: E402
from strategy_validation import integrity as IG  # noqa: E402
from strategy_validation import universe as UNI  # noqa: E402
from test_cross_sectional_641 import _as_v2, _row_hash, _store_raw  # noqa: E402
from value_quality.adapter import ValueQualityAdapter  # noqa: E402

AT = "2026-10-26T00:00:00+00:00"
POLICY_COMMIT = "a019740"
FROZEN_HASH = "b5d4a56ef5bba61f8250b3cd25182cc1655738adf0605a77671fd029883f8530"


def now_text(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8").replace("\r\n", "\n")


def decl(method="POINT_IN_TIME", complete=True, start="2018-01-01", end="2030-01-01", **over):
    d = {"universe_method": method, "universe_source": "synthetic security master", "membership_as_of_semantics": "membership known at each rebalance",
         "includes_delisted": complete, "includes_acquired": complete, "includes_bankrupt": complete, "historical_additions_supported": complete,
         "historical_removals_supported": complete, "survivorship_bias": "ABSENT" if method in ("POINT_IN_TIME", "HISTORICAL_SNAPSHOT") else "PRESENT",
         "coverage_start": start, "coverage_end": end, "notes": "test"}
    return {**d, **over}


PIT = UNI.PointInTimeMembership([
    {"security_id": "S1", "effective_from": "2019-01-01", "effective_to": None, "removal_reason": None},
    {"security_id": "S2", "effective_from": "2021-06-01", "effective_to": None, "removal_reason": None},                      # a later addition
    {"security_id": "S3", "effective_from": "2019-01-01", "effective_to": "2022-03-15", "removal_reason": "DELISTED"},
    {"security_id": "S4", "effective_from": "2019-01-01", "effective_to": "2021-11-01", "removal_reason": "ACQUIRED"},
    {"security_id": "S5", "effective_from": "2019-01-01", "effective_to": "2020-08-01", "removal_reason": "BANKRUPT"},
    {"security_id": "S6", "effective_from": "2019-01-01", "effective_to": None, "removal_reason": None,
     "symbols": [{"symbol": "OLD", "from": "2019-01-01", "to": "2022-01-10"}, {"symbol": "NEW", "from": "2022-01-10", "to": None}]},
])


@functools.lru_cache(maxsize=None)
def mom_run():
    ds = MF.dataset(MF.universe(25))
    return ds, E.run_validation(MomentumAdapter({"top_n": 5}), ds, registry=R.by_id(), created_at=AT)


def rec_of(run) -> dict:
    return json.loads(run.model_dump_json())


# ================================================================ A-C classification ===============================================================

def test_A01_a_fixed_current_universe_is_untrusted_and_both_strategies_declare_one():
    for ad in (ValueQualityAdapter(), MomentumAdapter()):
        d = ad.universe_declaration
        assert UNI.validate_declaration(d) == [] and d["universe_method"] == "FIXED_CURRENT_UNIVERSE" and d["survivorship_bias"] == "PRESENT"
        assert d["includes_delisted"] is False and d["historical_removals_supported"] is False and d["coverage_start"] is None and d["coverage_end"] is None
        c = UNI.classify(d, None, None, "2020-07-27", "2026-10-23")
        assert (c["status"], c["cap"], c["point_in_time"], c["policy_version"]) == ("UNTRUSTED", "NOT_ESTABLISHED", False, UNIVERSE_POLICY_VERSION)
    with pytest.raises(AssertionError):
        assert UNI.validate_declaration(decl("FIXED_CURRENT_UNIVERSE", survivorship_bias="ABSENT")) == []   # a fixed universe cannot claim no bias


SEL = [{"rebalance": "2019-02-01", "eligible": ["S1", "S3", "S4", "S5", "OLD"], "selected": ["S1"]},
       {"rebalance": "2021-12-01", "eligible": ["S1", "S2", "S3", "OLD"], "selected": ["S2"]},
       {"rebalance": "2022-06-01", "eligible": ["S1", "S2", "NEW"], "selected": ["NEW"]}]


def test_B01_point_in_time_membership_is_trusted_only_when_supplied_complete_covering_and_ranked():
    W = ("2019-01-02", "2022-12-30")
    assert UNI.classify(decl(), PIT, None, *W, selections=SEL)["status"] == "TRUSTED"
    chk = UNI.classify(decl(), PIT, None, *W, selections=SEL)["membership_check"]
    assert (chk["rebalances"], chk["rebalances_checked"], chk["rebalances_outside"], chk["rebalances_unaccounted"], chk["members_without_data"]) == \
        (["2019-02-01", "2021-12-01", "2022-06-01"], 3, 0, 0, 0)
    assert UNI.classify(decl(), None, None, *W)["status"] == "UNTRUSTED"                                         # claimed, not supplied
    assert UNI.classify(decl(), PIT, None, *W)["status"] == "UNTRUSTED"                                          # supplied, but not checked against the ranking
    assert UNI.classify(decl(complete=False), PIT, None, *W, selections=SEL)["status"] == "LIMITED"
    assert UNI.classify(decl(includes_bankrupt=None), PIT, None, *W, selections=SEL)["status"] == "LIMITED"         # unknown is not true
    assert UNI.classify(decl(start="2020-01-01"), PIT, None, *W, selections=SEL)["status"] == "LIMITED"             # coverage starts too late
    snaps = UNI.SnapshotMembership({"2019-01-01": ["A"], "2019-02-01": ["A", "B"], "2019-03-01": ["B"]})
    ssel = [{"rebalance": "2019-01-02", "eligible": ["A"], "selected": ["A"]}, {"rebalance": "2019-02-01", "eligible": ["A", "B"], "selected": ["B"]},
            {"rebalance": "2019-03-04", "eligible": ["B"], "selected": ["B"]}]
    assert UNI.classify(decl("HISTORICAL_SNAPSHOT"), snaps, ["2019-01-02", "2019-02-01", "2019-03-04"], "2019-01-02", "2019-03-29", ssel)["status"] == "TRUSTED"
    late = UNI.classify(decl("HISTORICAL_SNAPSHOT"), snaps, ["2019-01-02", "2019-06-03"], "2019-01-02", "2019-06-28", ssel[:1])
    assert late["status"] == "LIMITED" and f"within {SNAPSHOT_MAX_STALENESS_DAYS} days" in " ".join(late["reasons"])
    assert UNI.classify(decl("HISTORICAL_SNAPSHOT"), None, None, "2019-01-02", "2019-03-29")["status"] == "UNTRUSTED"   # claimed, none supplied


def test_B02_a_valid_history_beside_a_ranking_of_todays_list_is_survivorship_leakage():
    leak = SEL[:1] + [{"rebalance": "2021-12-01", "eligible": ["S1", "S2", "S3", "S4", "OLD"], "selected": ["S4"]}]   # S4 was acquired 2021-11-01
    c = UNI.classify(decl(), PIT, None, "2019-01-02", "2022-12-30", selections=leak)
    assert c["status"] == "UNTRUSTED" and "survivorship leakage" in c["reasons"][0] and c["membership_check"]["first_outside"]["symbols"] == ["S4"]
    early = [{"rebalance": "2021-05-03", "eligible": ["S1", "S2"], "selected": ["S2"]}]                                 # S2 joins 2021-06-01
    assert UNI.classify(decl(), PIT, None, "2019-01-02", "2022-12-30", selections=early)["status"] == "UNTRUSTED"
    rec = UNI.classify(decl(), PIT, None, "2019-01-02", "2022-12-30", selections=SEL)
    assert UNI.reclassify(rec, "2019-01-02", "2022-12-30") == ("TRUSTED",)
    assert UNI.reclassify({**rec, "membership_check": None}, "2019-01-02", "2022-12-30") == ("UNTRUSTED",)          # the gate needs the recorded check
    assert UNI.reclassify({**rec, "membership_check": {**rec["membership_check"], "rebalances_outside": 1}}, "2019-01-02", "2022-12-30") == ("UNTRUSTED",)


def test_B03_every_member_is_ranked_or_excluded_with_a_reason_and_trusted_needs_an_absent_bias():
    W = ("2019-01-02", "2022-12-30")
    silent = [{"rebalance": "2021-12-01", "eligible": ["S1", "S2", "OLD"], "selected": ["S2"]}]           # S3 (delisted 2022-03-15) silently never ranks
    c = UNI.classify(decl(), PIT, None, *W, selections=silent)
    assert c["status"] == "UNTRUSTED" and c["membership_check"]["first_unaccounted"]["symbols"] == ["S3"] and "survivorship leakage" in c["reasons"][0]
    c = UNI.classify(decl(), PIT, None, *W, selections=[{**silent[0], "excluded": {"S3": "NO_DATA"}}])      # excluded for missing data: its outcome is missing
    assert c["status"] == "LIMITED" and any("missing price data" in r for r in c["reasons"])
    assert UNI.classify(decl(), PIT, None, *W, selections=[{**silent[0], "excluded": {"S3": "PRICE_BELOW_5"}}])["status"] == "TRUSTED"   # a stated rule
    assert UNI.classify(decl(survivorship_bias="UNKNOWN"), PIT, None, *W, selections=SEL)["status"] == "LIMITED"
    no_rm = UNI.PointInTimeMembership([{"security_id": "S1", "effective_from": "2019-01-01", "effective_to": None, "removal_reason": None}])
    c = UNI.classify(decl(), no_rm, None, *W, selections=[{"rebalance": "2020-01-02", "eligible": ["S1"], "selected": ["S1"]}])
    assert c["status"] == "LIMITED" and any("no removal at all" in r for r in c["reasons"])                # completeness claimed, nothing ever removed
    assert UNI.classify(decl(), PIT, None, *W, selections=[])["status"] == "UNTRUSTED"                       # nothing checked
    rec = UNI.classify(decl(), PIT, None, *W, selections=SEL)
    assert UNI.reclassify(rec, *W, periods=["2019-02-01", "2021-12-01"]) == ("TRUSTED",)
    assert UNI.reclassify(rec, *W, periods=["2019-02-01", "2020-03-02"]) == ("UNTRUSTED",)               # a period the check never saw
    assert UNI.reclassify({**rec, "declaration": decl(survivorship_bias="UNKNOWN")}, *W) == ("LIMITED",)
    assert UNI.reclassify({**rec, "membership": {"kind": "POINT_IN_TIME"}}, *W) == ("UNTRUSTED",)         # a bare summary is not a membership


def test_C01_unknown_or_missing_is_never_trusted():
    assert UNI.classify(decl("UNKNOWN", survivorship_bias="UNKNOWN"), None)["status"] == "UNTRUSTED"
    miss = UNI.classify(None, None)
    assert miss["status"] is None and miss.get("missing") and UNI.cap_edge({"status": "ESTABLISHED"}, miss)["status"] == "INVALID"
    bad = UNI.classify({"universe_method": "POINT_IN_TIME"}, PIT)
    assert bad["status"] is None and bad.get("invalid") and UNI.cap_edge({"status": "WEAK"}, bad)["reasons"][0]["code"] == "UNIVERSE_NOT_DECLARED"
    assert set(UNIVERSE_METHODS) == {"POINT_IN_TIME", "HISTORICAL_SNAPSHOT", "FIXED_CURRENT_UNIVERSE", "UNKNOWN"}
    assert set(UNIVERSE_INTEGRITY_STATUSES) == {"TRUSTED", "LIMITED", "UNTRUSTED", "NOT_APPLICABLE"}


# ================================================================ D-E the cap ======================================================================

def test_D01_the_cap_lowers_never_raises_and_keeps_the_raw_edge():
    T, L, U = {"status": "TRUSTED", "cap": None}, {"status": "LIMITED", "cap": "WEAK", "method": "HISTORICAL_SNAPSHOT"}, {"status": "UNTRUSTED", "cap": "NOT_ESTABLISHED", "method": "FIXED_CURRENT_UNIVERSE"}
    for raw, ui, final in (("ESTABLISHED", T, "ESTABLISHED"), ("ESTABLISHED", L, "WEAK"), ("ESTABLISHED", U, "NOT_ESTABLISHED"), ("WEAK", L, "WEAK"),
                           ("WEAK", U, "NOT_ESTABLISHED"), ("NOT_ESTABLISHED", T, "NOT_ESTABLISHED"), ("INSUFFICIENT_DATA", U, "INSUFFICIENT_DATA"),
                           ("INVALID", U, "INVALID")):
        out = UNI.cap_edge({"status": raw, "reasons": [{"code": "X", "text": "raw"}]}, ui)
        assert out["status"] == final and out["raw_status"] == raw and out["capped"] is (final != raw), (raw, ui["status"])
        if out["capped"]:
            assert out["reasons"][0]["code"] == "UNIVERSE_INTEGRITY_CAP" and out["reasons"][1]["code"] == "X"
    assert EDGE_LADDER == ("NOT_ESTABLISHED", "WEAK", "ESTABLISHED") and UNIVERSE_EDGE_CAP == {"TRUSTED": None, "LIMITED": "WEAK", "UNTRUSTED": "NOT_ESTABLISHED",
                                                                                                 "NOT_APPLICABLE": None}


def test_D02_a_raw_established_edge_on_the_fixed_universe_is_final_not_established_and_never_PASS():
    ds, run = mom_run()                                                                             # synthetic momentum with a designed edge
    assert run.cross_sectional["selection_edge"]["status"] == "ESTABLISHED"                       # the raw v3 edge
    assert run.selection_edge["status"] == "NOT_ESTABLISHED" and run.selection_edge["capped"] and run.selection_edge["raw_status"] == "ESTABLISHED"
    assert run.selection_edge["reasons"][0]["code"] == "UNIVERSE_INTEGRITY_CAP" and run.universe_integrity["status"] == "UNTRUSTED"
    assert run.verdict["verdict"] == "FAIL" and run.verdict["eligible_for_review"] is False
    pv = copy.deepcopy(run.performance_verdict)
    assert CS.overall_verdict(pv, "ESTABLISHED")["verdict"] == "PASS"                              # what the raw edge alone would have read


def test_E01_absolute_performance_and_the_control_comparison_stay_intact():
    ds, run = mom_run()
    assert run.performance_verdict["verdict"] == "PASS" and run.arms["RULES"].verdict["verdict"] == "PASS" and run.arms["RULES"].metrics["OOS"]["net_pnl"] > 0
    assert run.cross_sectional["parity"]["status"] == "PASS" and run.cross_sectional["segments"]["OOS"]["period_count"] > 0
    assert IG.evaluate(rec_of(run)).status == "ELIGIBLE"                                            # a valid replay on a biased universe still persists


# ================================================================ F-G history is never rewritten ===================================================

def _assess_and_store(tmp_path, monkeypatch, adapter, ds):
    from strategy_validation.store import StrategyValidationStore
    monkeypatch.setenv("STOCK_AGENT_ENV", "test")
    run = E.run_validation(adapter, ds, registry=R.by_id(), created_at=AT)
    old = _as_v2(run)                                                                                # a record from before Stage 6.5.1
    old.pop("universe_integrity", None)
    db = tmp_path / "v.db"
    _store_raw(db, old)
    before = _row_hash(db, old["run_id"])
    store = StrategyValidationStore(db)
    stored = store.run(old["run_id"])
    ev = CS.assess_stored_run(stored, store.candidates(old["run_id"]), store.trades(old["run_id"], "RULES"), ds, adapter, registry=R.by_id(), created_at=AT)
    store.insert_evaluation(ev)
    a = UNI.assess_stored_run(store.run(old["run_id"]), adapter.universe_declaration, store.latest_evaluation(old["run_id"]), created_at=AT)
    return store, db, old, before, ev, a


def test_F01_value_quality_history_gets_an_append_only_assessment_and_no_outcome_improves(tmp_path, monkeypatch):
    from strategy_validation import service as SV
    ds = VF.dataset(VF.run_set(), end="2022-12-30")
    store, db, old, before, ev, a = _assess_and_store(tmp_path, monkeypatch, ValueQualityAdapter({"top_n": 5}), ds)
    assert a["raw_selection_edge"]["status"] == ev["selection_edge"]["status"] and a["universe_integrity"]["status"] == "UNTRUSTED"
    assert a["selection_edge"]["status"] in ("NOT_ESTABLISHED", "INSUFFICIENT_DATA", "INVALID") and a["verdict"]["verdict"] != "PASS"
    store.insert_universe_assessment(a)
    assert _row_hash(db, old["run_id"]) == before and store.latest_evaluation(old["run_id"]) == ev   # run and evaluation unchanged
    s = SV.summary(store.latest("VALUE_QUALITY"), "VALUE_QUALITY")
    assert (s["universe_integrity_status"], s["universe_method"], s["raw_selection_edge_status"]) == ("UNTRUSTED", "FIXED_CURRENT_UNIVERSE", ev["selection_edge"]["status"])
    assert s["validation_status"] == a["verdict"]["verdict"] and s["selection_edge_status"] == a["selection_edge"]["status"]
    with sqlite3.connect(str(db)) as conn:
        for sql in ("UPDATE strategy_validation_universe_assessments SET verdict = 'PASS'", "DELETE FROM strategy_validation_universe_assessments"):
            with pytest.raises(sqlite3.DatabaseError):
                conn.execute(sql)


def test_G01_momentum_history_gets_an_append_only_assessment_and_no_outcome_improves(tmp_path, monkeypatch):
    from strategy_validation.store import StrategyValidationStore
    monkeypatch.setenv("STOCK_AGENT_ENV", "test")
    ds, run = mom_run()
    old = rec_of(run)                                                                                # a Stage 6.5 v3 record: no universe fields yet
    old.pop("universe_integrity")
    old["selection_edge"] = old["cross_sectional"]["selection_edge"]
    old["verdict"] = CS.overall_verdict(old["performance_verdict"], old["selection_edge"]["status"])
    db = tmp_path / "m.db"
    _store_raw(db, old)
    before = _row_hash(db, old["run_id"])
    store = StrategyValidationStore(db)
    stored = store.run(old["run_id"])
    assert stored["verdict"]["verdict"] == "PASS"                                                    # (synthetic, designed edge) as recorded under v3
    from strategy_validation import service as SV
    s0 = SV.summary(store.latest("MOMENTUM"), "MOMENTUM")                                             # not yet assessed: read as UNKNOWN, never uncapped
    assert (s0["validation_status"], s0["selection_edge_status"], s0["raw_selection_edge_status"], s0["universe_integrity_status"], s0["universe_method"]) == \
        ("FAIL", "NOT_ESTABLISHED", "ESTABLISHED", "UNTRUSTED", "UNKNOWN")
    assert SV.detail(store.latest("MOMENTUM"), "MOMENTUM", R.get("MOMENTUM"))["universe_integrity"]["source"] == "not_assessed"
    a = UNI.assess_stored_run(stored, MomentumAdapter().universe_declaration, None, created_at=AT)
    assert (a["raw_selection_edge"]["status"], a["selection_edge"]["status"], a["verdict"]["verdict"]) == ("ESTABLISHED", "NOT_ESTABLISHED", "FAIL")
    store.insert_universe_assessment(a)
    assert _row_hash(db, old["run_id"]) == before and store.run(old["run_id"])["verdict"]["verdict"] == "PASS"   # the record keeps its own verdict
    latest = store.latest("MOMENTUM")
    assert latest["universe_assessment"]["assessment_id"] == a["assessment_id"]
    assert SV.summary(latest, "MOMENTUM")["universe_method"] == "FIXED_CURRENT_UNIVERSE" and SV.detail(latest, "MOMENTUM")["universe_integrity"]["source"] == "assessment"


def test_G03_an_evaluation_made_after_651_is_capped_until_assessed_and_an_assessment_follows_the_newest_evaluation(tmp_path, monkeypatch):
    from strategy_validation import service as SV
    from strategy_validation.store import StoreError, StrategyValidationStore
    monkeypatch.setenv("STOCK_AGENT_ENV", "test")
    ds, run = mom_run()
    old = _as_v2(run)                                                                                # a validation_v2 Momentum record (no v3 / universe fields)
    old.pop("universe_integrity")
    db = tmp_path / "e.db"
    _store_raw(db, old)
    store = StrategyValidationStore(db)
    ad = MomentumAdapter({"top_n": 5})
    ev1 = CS.assess_stored_run(store.run(old["run_id"]), store.candidates(old["run_id"]), store.trades(old["run_id"], "RULES"), ds, ad, registry=R.by_id(), created_at=AT)
    store.insert_evaluation(ev1)
    assert (ev1["selection_edge"]["status"], ev1["verdict"]["verdict"]) == ("ESTABLISHED", "PASS")    # the v3 evaluation itself is uncapped ...
    s = SV.summary(store.latest("MOMENTUM"), "MOMENTUM")
    assert (s["validation_status"], s["selection_edge_status"], s["universe_integrity_status"]) == ("FAIL", "NOT_ESTABLISHED", "UNTRUSTED")   # ... never read so
    a1 = UNI.assess_stored_run(store.run(old["run_id"]), ad.universe_declaration, store.latest_evaluation(old["run_id"]), created_at=AT)
    store.insert_universe_assessment(a1)
    ev2 = {**ev1, "evaluation_id": "f" * 32}
    store.insert_evaluation(ev2)                                                                     # a newer evaluation: the assessment no longer covers it
    assert SV.detail(store.latest("MOMENTUM"), "MOMENTUM")["universe_integrity"]["source"] == "not_assessed"
    stale = {**UNI.assess_stored_run(store.run(old["run_id"]), ad.universe_declaration, ev1, created_at=AT)}
    with pytest.raises(StoreError, match="newest methodology evaluation"):
        store.insert_universe_assessment(stale)


def _local_copy(tmp_path):
    src = ROOT / "runtime" / "preview_651.db"
    if not src.exists():
        pytest.skip("local data only: the stored Value + Quality and Momentum runs live in a git-ignored runtime database")
    dst = tmp_path / "local.db"
    shutil.copy(src, dst)
    return dst


@pytest.mark.parametrize("run_id,strategy", [("1660bd5091ad4799a6908b9e8ddc6f2f", "VALUE_QUALITY"), ("52922e3f66234c489d6816f3243bd139", "MOMENTUM")])
def test_F02_G02_the_real_results_where_the_local_data_exists(tmp_path, run_id, strategy):
    from strategy_validation.adapters import adapter_for
    from strategy_validation.store import StrategyValidationStore
    db = _local_copy(tmp_path)
    store = StrategyValidationStore(db)
    run = store.run(run_id)
    if run is None:
        pytest.skip("not in this local database")
    before = _row_hash(db, run_id)
    ad, _ = adapter_for(strategy, R.get(strategy))
    a = UNI.assess_stored_run(run, ad.universe_declaration, store.latest_evaluation(run_id), created_at=AT)
    store.insert_universe_assessment(a)
    assert _row_hash(db, run_id) == before
    assert (a["performance_verdict"]["verdict"], a["raw_selection_edge"]["status"], a["universe_integrity"]["status"], a["selection_edge"]["status"],
            a["verdict"]["verdict"]) == ("PASS", "NOT_ESTABLISHED", "UNTRUSTED", "NOT_ESTABLISHED", "FAIL")
    assert R.get(strategy).status == "RESEARCH"


# ================================================================ H-J declarations, event-driven, control ===========================================

def test_H01_a_cross_sectional_run_without_a_declaration_is_blocked(tmp_path, monkeypatch):
    from strategy_validation.store import PersistenceBlocked, StrategyValidationStore
    monkeypatch.setenv("STOCK_AGENT_ENV", "test")
    bare = type("Bare", (MomentumAdapter,), {"universe_declaration": None})({"top_n": 5})
    run = E.run_validation(bare, MF.dataset(MF.universe(25)), registry=R.by_id(), created_at=AT)
    assert run.selection_edge["status"] == "INVALID" and run.selection_edge["reasons"][0]["code"] == "UNIVERSE_NOT_DECLARED"
    el = IG.evaluate(rec_of(run))
    assert el.status == "BLOCKED" and {c.check: c.status for c in el.checks}["UNIVERSE_INTEGRITY"] == "FAIL"
    with pytest.raises(PersistenceBlocked):
        StrategyValidationStore(tmp_path / "v.db").insert(run)
    stripped = rec_of(mom_run()[1])
    stripped.pop("universe_integrity")                                                               # never a silent default to trusted
    assert {c.check: c.status for c in IG.evaluate(stripped).checks}["UNIVERSE_INTEGRITY"] == "FAIL"


def test_I01_event_driven_strategies_are_unaffected():
    ds, ad = FX.fixture("GOOD")
    r = E.run_validation(ad, ds, created_at=AT)
    assert r.universe_integrity["status"] == "NOT_APPLICABLE" and r.selection_edge["status"] == "NOT_APPLICABLE" and r.verdict["verdict"] == "PASS"
    assert {c.check: c.status for c in IG.evaluate(rec_of(r)).checks}["UNIVERSE_INTEGRITY"] == "NOT_APPLICABLE"


def test_J01_a_fair_control_does_not_clear_survivorship_bias():
    ds, run = mom_run()
    assert run.cross_sectional["parity"]["status"] == "PASS"                                        # the control is fair (8 / 8) ...
    assert run.universe_integrity["status"] == "UNTRUSTED" and run.selection_edge["status"] == "NOT_ESTABLISHED"   # ... the universe still caps the edge
    assert UNI.reclassify(run.universe_integrity, run.dataset_start, run.dataset_end) == ("UNTRUSTED",)   # parity is not an input of the integrity
    assert "does not remove survivorship bias" in now_text("frontend/product_ui.js") and "does not remove survivorship bias" in now_text("DESIGN_651_UNIVERSE_INTEGRITY.md")


# ================================================================ K-M the point-in-time contract =====================================================

def test_K01_future_dated_membership_never_appears_before_its_effective_date():
    assert "S2" not in PIT.members_as_of("2021-05-31") and "S2" in PIT.members_as_of("2021-06-01")
    with pytest.raises(UNI.UniverseError):
        UNI.PointInTimeMembership([{"security_id": "X", "effective_from": "2021-01-01", "effective_to": "2020-01-01", "removal_reason": "DELISTED"}])
    with pytest.raises(UNI.UniverseError):
        UNI.PointInTimeMembership([{"security_id": "X", "effective_from": "2020-01-01", "effective_to": None, "removal_reason": None},
                                   {"security_id": "X", "effective_from": "2021-01-01", "effective_to": None, "removal_reason": None}])   # overlap
    for bad in ("20220601", "2022-W22-3", "2022-6-1"):                                              # string order is date order only for YYYY-MM-DD
        with pytest.raises(UNI.UniverseError):
            UNI.PointInTimeMembership([{"security_id": "X", "effective_from": "2019-01-01", "effective_to": bad, "removal_reason": "DELISTED"}])
        with pytest.raises(UNI.UniverseError):
            PIT.members_as_of(bad)
    assert UNI.validate_declaration(decl(start="20180101")) != []


def test_L01_a_delisted_or_bankrupt_member_is_a_member_until_its_removal():
    assert "S3" in PIT.members_as_of("2022-03-14") and "S3" not in PIT.members_as_of("2022-03-15")
    assert "S5" in PIT.members_as_of("2020-07-31") and "S5" not in PIT.members_as_of("2020-08-01")
    with pytest.raises(UNI.UniverseError):
        UNI.PointInTimeMembership([{"security_id": "X", "effective_from": "2020-01-01", "effective_to": "2021-01-01", "removal_reason": None}])   # no reason


def test_M01_an_acquired_member_disappears_only_from_its_removal_date_and_a_renamed_one_keeps_its_identity():
    assert PIT.members_as_of("2021-10-29").get("S4") == "S4" and "S4" not in PIT.members_as_of("2021-11-01")
    assert PIT.members_as_of("2022-01-07")["S6"] == "OLD" and PIT.members_as_of("2022-01-10")["S6"] == "NEW"
    assert set(PIT.members_as_of("2022-06-01")) == {"S1", "S2", "S6"} and PIT.summary()["removals"] == 3
    reuse = UNI.PointInTimeMembership([                                                               # a ticker reused by a different security later
        {"security_id": "A", "effective_from": "2019-01-01", "effective_to": "2021-06-01", "removal_reason": "DELISTED", "symbols": [{"symbol": "XYZ", "from": "2019-01-01", "to": "2021-06-01"}]},
        {"security_id": "B", "effective_from": "2023-01-02", "effective_to": None, "removal_reason": None, "symbols": [{"symbol": "XYZ", "from": "2023-01-02", "to": None}]}])
    assert reuse.members_as_of("2020-01-02") == {"A": "XYZ"} and reuse.members_as_of("2022-01-03") == {} and reuse.members_as_of("2023-02-01") == {"B": "XYZ"}
    with pytest.raises(UNI.UniverseError, match="at the same time"):
        UNI.PointInTimeMembership([{"security_id": "A", "effective_from": "2019-01-01", "effective_to": None, "removal_reason": None, "symbols": [{"symbol": "XYZ", "from": "2019-01-01", "to": None}]},
                                   {"security_id": "B", "effective_from": "2020-01-01", "effective_to": None, "removal_reason": None, "symbols": [{"symbol": "XYZ", "from": "2020-01-01", "to": None}]}])
    with pytest.raises(UNI.UniverseError, match="overlapping symbol"):
        UNI.PointInTimeMembership([{"security_id": "A", "effective_from": "2019-01-01", "effective_to": None, "removal_reason": None,
                                    "symbols": [{"symbol": "P", "from": "2019-01-01", "to": "2021-01-01"}, {"symbol": "Q", "from": "2020-06-01", "to": None}]}])


# ================================================================ N-Z isolation, gate, store, CLI, payload, documents ================================

def test_N01_no_broker_execution_or_network_import():
    for rel in ("strategy_validation/universe.py", "strategy_validation/cli.py", "strategy_validation/store.py", "strategy_validation/integrity.py"):
        tree = ast.parse(now_text(rel))
        mods = {n.module or "" for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)} | {a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
        assert not any(m.startswith(("execution", "safety", "paper", "portfolio", "proposals", "api", "requests", "urllib", "httpx", "socket", "alpaca", "anthropic"))
                       for m in mods), (rel, mods)


def test_O01_the_gate_re_derives_the_integrity_and_the_cap():
    ds, run = mom_run()
    rec = rec_of(run)
    forged = copy.deepcopy(rec)
    forged["universe_integrity"]["status"], forged["universe_integrity"]["cap"] = "TRUSTED", None    # a fixed universe passed off as trusted
    assert IG.evaluate(forged).status == "BLOCKED"
    uncapped = copy.deepcopy(rec)
    uncapped["selection_edge"] = uncapped["cross_sectional"]["selection_edge"]                       # the raw edge presented as final
    uncapped["verdict"] = CS.overall_verdict(uncapped["performance_verdict"], "ESTABLISHED")
    el = IG.evaluate(uncapped)
    assert el.status == "BLOCKED" and any(r["code"] == "SELECTION_CONTROL" for r in el.reasons)
    pit_claim = copy.deepcopy(rec)
    pit_claim["universe_integrity"]["declaration"] = decl()                                          # a point-in-time claim with no membership supplied
    pit_claim["universe_integrity"]["status"], pit_claim["universe_integrity"]["cap"] = "TRUSTED", None
    assert IG.evaluate(pit_claim).status == "BLOCKED"
    periods = [p["rebalance"] for p in rec["cross_sectional"]["periods"]]
    thin = copy.deepcopy(rec)                                                                         # a bare membership summary, one rebalance "checked"
    thin["universe_integrity"].update({"declaration": decl(start="2000-01-01", end="2099-12-31"), "method": "POINT_IN_TIME", "status": "TRUSTED", "cap": None,
                                       "membership": {"kind": "POINT_IN_TIME"}, "membership_check": {"rebalances_checked": 1, "rebalances_outside": 0}})
    thin["selection_edge"] = UNI.cap_edge(thin["cross_sectional"]["selection_edge"], thin["universe_integrity"])
    thin["verdict"] = CS.overall_verdict(thin["performance_verdict"], thin["selection_edge"]["status"])
    assert thin["verdict"]["verdict"] == "PASS" and IG.evaluate(thin).status == "BLOCKED"
    full = copy.deepcopy(thin)                                                                        # a complete-looking claim the adapter never made
    full["universe_integrity"].update({"membership": PIT.summary(), "membership_check": {"rebalances": periods, "rebalances_checked": len(periods), "rebalances_outside": 0,
                                                                                         "rebalances_unaccounted": 0, "members_without_data": 0}})
    el = IG.evaluate(full)
    assert el.status == "BLOCKED" and "not the adapter's own" in {c.check: c.detail for c in el.checks}["UNIVERSE_INTEGRITY"]


def test_O02_the_store_binds_a_registered_strategys_run_to_its_registered_declaration(tmp_path, monkeypatch):
    from strategy_validation.store import StoreError, StrategyValidationStore
    monkeypatch.setenv("STOCK_AGENT_ENV", "test")
    other = {**MomentumAdapter.universe_declaration, "universe_source": "a different list"}
    run = E.run_validation(type("Other", (MomentumAdapter,), {"universe_declaration": other})({"top_n": 5}), MF.dataset(MF.universe(25)), registry=R.by_id(), created_at=AT)
    assert IG.evaluate(rec_of(run)).status == "ELIGIBLE"                                              # consistent with its own config snapshot ...
    with pytest.raises(StoreError, match="registered strategy's own"):                               # ... but not the registered MOMENTUM declaration
        StrategyValidationStore(tmp_path / "v.db").insert(run)


def test_P01_the_store_refuses_forged_or_inconsistent_assessments(tmp_path, monkeypatch):
    from strategy_validation.store import StoreError, StrategyValidationStore
    monkeypatch.setenv("STOCK_AGENT_ENV", "test")
    ds, run = mom_run()
    old = rec_of(run)
    old.pop("universe_integrity")
    old["selection_edge"] = old["cross_sectional"]["selection_edge"]
    old["verdict"] = CS.overall_verdict(old["performance_verdict"], "ESTABLISHED")
    _store_raw(tmp_path / "v.db", old)
    store = StrategyValidationStore(tmp_path / "v.db")
    good = UNI.assess_stored_run(store.run(old["run_id"]), MomentumAdapter().universe_declaration, None, created_at=AT)
    forged = UNI.assess_stored_run(store.run(old["run_id"]), decl(), None, created_at=AT)              # a different, point-in-time declaration
    with pytest.raises(StoreError, match="registered strategy's own"):
        store.insert_universe_assessment(forged)
    uncapped = {**good, "assessment_id": "a" * 32, "selection_edge": good["raw_selection_edge"], "verdict": CS.overall_verdict(good["performance_verdict"], "ESTABLISHED")}
    with pytest.raises(StoreError, match="do not follow the cap"):
        store.insert_universe_assessment(uncapped)
    with pytest.raises(StoreError, match="in the future"):
        store.insert_universe_assessment({**good, "assessment_id": "b" * 32, "created_at": "9999-01-01T00:00:00+00:00"})
    tampered = copy.deepcopy(good)                                                                   # the statuses right, the displayed fields forged
    tampered.update({"assessment_id": "c" * 32})
    tampered["universe_integrity"].update({"point_in_time": True, "survivorship_bias": "ABSENT", "consequence": "The v3 selection-edge policy applies in full."})
    with pytest.raises(StoreError, match="every field is re-derived"):
        store.insert_universe_assessment(tampered)
    relabel = copy.deepcopy(good)
    relabel["assessment_id"], relabel["verdict"]["label"] = "d" * 32, "PASS"
    with pytest.raises(StoreError, match="do not follow the cap"):
        store.insert_universe_assessment(relabel)
    store.insert_universe_assessment(good)
    v3 = tmp_path / "v3.db"
    StrategyValidationStore(v3).insert(run)
    with pytest.raises(UNI.UniverseError, match="already carries"):
        UNI.assess_stored_run(StrategyValidationStore(v3).run(run.run_id), MomentumAdapter().universe_declaration)


def test_Q01_cli_assess_universe_reports_and_stores(tmp_path, monkeypatch):
    from strategy_validation import cli
    monkeypatch.setenv("STOCK_AGENT_ENV", "test")
    ds, run = mom_run()
    old = rec_of(run)
    old.pop("universe_integrity")
    old["selection_edge"] = old["cross_sectional"]["selection_edge"]
    old["verdict"] = CS.overall_verdict(old["performance_verdict"], "ESTABLISHED")
    _store_raw(tmp_path / "v.db", old)
    out = io.StringIO()
    code = cli.main(["assess-universe", "--run", old["run_id"], "--db", str(tmp_path / "v.db"), "--persist", "--output", str(tmp_path / "a.json")], out=out)
    t = out.getvalue()
    a = json.loads((tmp_path / "a.json").read_text(encoding="utf-8"))
    assert code == 0 and "universe FIXED_CURRENT_UNIVERSE -> UNTRUSTED; raw selection edge ESTABLISHED -> final NOT_ESTABLISHED; overall " in t, t
    assert a["run_id"] == old["run_id"] and a["verdict"]["verdict"] == "FAIL" and f"overall {a['verdict']['label']}" in t and "stored universe assessment" in t
    assert cli.main(["assess-universe", "--run", "0" * 32, "--db", str(tmp_path / "v.db")], out=io.StringIO()) == 1


def test_R01_the_strategy_lab_payload_and_page_show_the_universe_integrity(tmp_path, monkeypatch):
    from strategy_validation import service as SV
    from strategy_validation.store import StrategyValidationStore
    monkeypatch.setenv("STOCK_AGENT_ENV", "test")
    ds, run = mom_run()
    store = StrategyValidationStore(tmp_path / "v.db")
    store.insert(run)
    d = SV.detail(store.latest("MOMENTUM"), "MOMENTUM", R.get("MOMENTUM"))
    u = d["universe_integrity"]
    assert (u["method"], u["status"], u["survivorship_bias"], u["includes_delisted"], u["raw_edge_status"], u["final_edge_status"], u["source"]) == \
        ("FIXED_CURRENT_UNIVERSE", "UNTRUSTED", "PRESENT", False, "ESTABLISHED", "NOT_ESTABLISHED", "run")
    assert d["universe_integrity_status"] == "UNTRUSTED" and d["selection_edge_status"] == "NOT_ESTABLISHED" and d["raw_selection_edge_status"] == "ESTABLISHED"
    js = "const P=require(process.argv[1]); const d=JSON.parse(require('fs').readFileSync(process.argv[2],'utf8')); process.stdout.write(P.validationHtml(d) + '\\n' + P.svBadge(d));"
    (tmp_path / "d.json").write_text(json.dumps(d), encoding="utf-8")
    r = subprocess.run(["node", "-e", js, str(ROOT / "frontend" / "product_ui.js"), str(tmp_path / "d.json")], capture_output=True, text=True, encoding="utf-8", cwd=ROOT)
    h = r.stdout
    assert r.returncode == 0 and 'data-pui-sv-section="universe"' in h and 'data-pui-sv-universe="UNTRUSTED"' in h, r.stderr[-400:]
    assert "Fixed current universe" in h and "Cannot establish alpha".lower() in h.lower() and "raw Established → final Not established (UNIVERSE_INTEGRITY_CAP)" in h
    assert h.count('data-pui-sv-universe-badge="UNTRUSTED"') == 2 and "does not remove survivorship bias" in h and "Eligible for review" not in h


def test_S01_node_tests_of_the_universe_display():
    r = subprocess.run(["node", "--test", str(ROOT / "tests" / "frontend" / "universe_integrity.test.cjs")], capture_output=True, text=True, cwd=ROOT)
    assert r.returncode == 0 and re.search(r"pass 4\b", r.stdout) and re.search(r"fail 0\b", r.stdout), r.stdout[-3000:] + r.stderr[-2000:]


def test_Z01_the_policy_was_committed_before_it_was_applied_and_no_threshold_changed():
    r = subprocess.run(["git", "show", f"{POLICY_COMMIT}:strategy_validation/__init__.py"], cwd=ROOT, capture_output=True, text=True, encoding="utf-8")
    if r.returncode != 0:
        pytest.skip("the policy commit is not reachable (shallow clone)")
    frozen: dict = {}
    exec(compile(r.stdout, "frozen", "exec"), frozen)                                              # noqa: S102 - the repository's own file at a fixed commit
    import strategy_validation as SVP
    for k in ("UNIVERSE_POLICY_VERSION", "UNIVERSE_METHODS", "UNIVERSE_INTEGRITY_STATUSES", "UNIVERSE_EDGE_CAP", "EDGE_LADDER", "SNAPSHOT_MAX_STALENESS_DAYS",
              "UNIVERSE_DECLARATION_FIELDS", "CS_MIN_OOS_PERIODS", "CS_MIN_TOTAL_PERIODS", "CS_T_NEGLIGIBLE", "CS_T_ESTABLISHED", "CS_TRAIN_CONTRADICTION_T",
              "CS_CONCENTRATION_SHARE", "VERSION", "DEFAULT_FRICTION"):
        assert frozen[k] == getattr(SVP, k), k
    import momentum as M
    import value_quality as VQ
    assert (M.LOOKBACK_SESSIONS, M.SKIP_SESSIONS, M.TOP_N, VQ.TOP_N, VQ.VALUE_WEIGHT) == (252, 21, 20, 20, 0.5)


def test_Z02_documents_and_frozen_controls():
    d = now_text("DESIGN_651_UNIVERSE_INTEGRITY.md")
    for s in ("## 1. The problem: survivorship bias", "A fair control does not remove it", "## 3. Integrity status", "## 4. The selection-edge cap",
              "## 5. The point-in-time contract", "## 7. Data readiness", "## 8. What SEC and Alpaca can and cannot give", "## 9. Possible future data sources",
              "## 10. Effect on Value + Quality and Momentum", "## 11. Adversarial review", "Do not use today's", "UNIVERSE_INTEGRITY_CAP"):
        assert s in d, s
    h = now_text("STRATEGY_VALIDATION_HARNESS.md")
    assert "## 15. Universe integrity (Stage 6.5.1, universe_integrity_v1)" in h and "UNIVERSE_INTEGRITY" in h
    readme = now_text("README.md")
    sec = re.findall(r"(?ms)^## Universe integrity \(Stage 6\.5\.1\)\n.*?(?=^## )", readme)
    assert len(sec) == 1 and "python -m strategy_validation assess-universe" in sec[0] and "UNTRUSTED" in sec[0]
    assert readme.index("## Momentum strategy (Stage 6.5") < readme.index("## Universe integrity (Stage 6.5.1)") < readme.index("## Continuous integration (Stage 6.0.3")
    assert not re.search(r"(?i)\b(guaranteed|profitable|buy now|sell now|regime)\b", sec[0] + h[h.index("## 15. Universe integrity"):])
    import execution as X
    import forward_validation as FV
    assert FV.frozen_hash() == FROZEN_HASH and X.MODES == ("MONITOR_ONLY", "PAPER_AUTO")
    assert "test_universe_integrity_651.py" in now_text(".github/ci/pytest_groups.json")
