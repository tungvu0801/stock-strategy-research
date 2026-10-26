"""
tests/test_validation_integrity_631.py — Stage 6.3.1 VALIDATION HARNESS HARDENING (DESIGN_631_VALIDATION_HARDENING.md).

  B  the TRAIN / OOS boundary PURGE: a TRAIN outcome that needs an OOS bar is purged (TRAIN_OOS_BOUNDARY_PURGE), the candidate stays TRAIN,
     and OOS prices cannot change a TRAIN metric (the key test: B03)
  A  the accounting invariant: accepted = simulated + boundary_purged + skipped; a mismatch is VALIDATION INVALID
  G  the persistence integrity gate: provenance, strategy version, candidate set, causality (automated + the declared manual review), replay
     parity, mechanical audit, accounting, boundary, structure - ELIGIBLE / BLOCKED with reasons; integrity is not the performance verdict
  S  the store enforces the gate itself (no bypass); Stage 6.2 records stay readable
  C  the CLI: --persist stores only an ELIGIBLE run (exit 3 otherwise), ephemeral runs still work
  D  Downside Dislocation Recovery stays NOT_RUN; no broker / execution import
"""
import ast
import json
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

import strategy_validation_fixtures as F  # noqa: E402
from strategy_validation import PURGE_REASON  # noqa: E402
from strategy_validation import engine as E  # noqa: E402
from strategy_validation import integrity as IG  # noqa: E402
from strategy_validation import service as SV  # noqa: E402
from strategy_validation import simulator as SIM  # noqa: E402
from strategy_validation import DEFAULT_FRICTION, DEFAULT_SIZING  # noqa: E402
from strategy_validation.audit import audit as run_audit  # noqa: E402
from strategy_validation.dataset import Dataset, dump  # noqa: E402

AT = "2026-10-25T00:00:00+00:00"
RATIO = 0.695                                     # 700 sessions -> TRAIN ends at session 485, inside candidate 69's window (signal 483)
METRIC_KEYS = ("trade_count", "win_rate", "gross_pnl", "slippage_cost", "fees", "net_pnl", "average_R", "expectancy_R", "expectancy_usd", "mean_R", "std_R",
               "mean_R_t_stat", "sharpe_per_trade", "max_drawdown", "max_drawdown_pct", "profit_factor", "best_trade", "worst_trade", "exit_reason_distribution")


def crossing(adapter_params=None, mutate_oos=None):
    """GOOD-like outcomes; candidate 69 is a 5-session time exit whose window straddles the TRAIN end (it cannot complete inside TRAIN)."""
    out = F.pattern(100, "WWWLL")
    out[69] = "F"
    ds, plan = F.build(out)
    if mutate_oos:
        ds = mutate_oos(ds)
    return ds, F.FixtureAdapter(plan, params=adapter_params)


def run(ds, ad, **kw):
    return E.run_validation(ad, ds, train_ratio=kw.pop("train_ratio", RATIO), created_at=AT, **kw)


def rec(r):
    return json.loads(r.model_dump_json())


def wild_oos(ds):
    """Every bar after the TRAIN end replaced by a wildly different price path (x5, a spike far above every target)."""
    from strategy_validation.dataset import split
    end = split(ds, RATIO)["train_end"]
    bars = {s: [b.model_dump() if b.date <= end else {**b.model_dump(), "open": b.open * 5, "high": b.high * 5.5, "low": b.low * 4.5, "close": b.close * 5}
                for b in rows] for s, rows in ds.bars.items()}
    return Dataset(bars, kind=ds.kind, source=ds.source, provenance=ds.declared)


# ================================================================ B the TRAIN / OOS boundary ======================================================

def test_B01_a_train_trade_crossing_into_oos_is_purged_with_an_explicit_reason():
    ds, ad = crossing()
    r = run(ds, ad)
    c69 = next(c for c in r.candidates if c.symbol == "S069")
    assert c69.signal_date <= r.train_end < ds.bars["S069"][-1].date                              # signal in TRAIN, the window continues into OOS
    rules = r.arms["RULES"]
    purge = [s for s in rules.simulation_skips if s.candidate_id == c69.candidate_id]
    assert [s.reason for s in purge] == [PURGE_REASON] and "reaches past the TRAIN" in purge[0].detail and r.train_end in purge[0].detail
    assert all(t.candidate_id != c69.candidate_id for t in rules.trades)                          # no trade: not relabelled OOS, not truncated
    assert r.boundary["purged"]["RULES"] == [c69.candidate_id] == r.boundary["purged"]["GATED"] and r.boundary["train_boundary_purged_count"] == 1
    assert r.boundary["policy"] == "PURGE" and r.diagnostics["boundary"]["train_boundary_purged_count"] == 1
    assert all(t.exit_date <= r.train_end for t in rules.trades if t.segment == "TRAIN")         # no kept TRAIN trade touches an OOS bar


def test_B02_the_candidate_stays_train_while_its_outcome_is_purged():
    ds, ad = crossing()
    r = run(ds, ad)
    tr, oo = r.arms["RULES"].metrics["TRAIN"], r.arms["RULES"].metrics["OOS"]
    assert (tr["candidate_count"], tr["accepted_count"], tr["simulated_trade_count"], tr["boundary_purged_count"], tr["simulation_skipped_count"]) == (70, 70, 69, 1, 0)
    assert (oo["candidate_count"], oo["simulated_trade_count"], oo["boundary_purged_count"]) == (30, 30, 0)   # never moved to OOS
    assert r.arms["RULES"].metrics["ALL"]["simulated_trade_count"] == 99 and r.arms["RULES"].metrics["ALL"]["boundary_purged_count"] == 1


def test_B03_oos_prices_cannot_change_train_metrics():
    """The key Stage 6.3.1 test: the same TRAIN, wildly different OOS prices -> identical TRAIN metrics (and the boundary trade is the one that
    would have moved: simulated on the full data its outcome differs between A and B)."""
    ds_a, ad_a = crossing()
    ds_b, ad_b = crossing(mutate_oos=wild_oos)
    a, b = run(ds_a, ad_a), run(ds_b, ad_b)
    assert a.train_end == b.train_end and a.candidate_set_hash == b.candidate_set_hash
    for arm in ("RULES", "GATED"):
        assert a.arms[arm].metrics["TRAIN"] == b.arms[arm].metrics["TRAIN"], arm                  # every TRAIN metric, every key
        assert a.arms[arm].metrics["OOS"] != b.arms[arm].metrics["OOS"]                           # the OOS prices really are different
    assert a.boundary["purged"] == b.boundary["purged"] and a.arms["RULES"].diagnostics["train_vs_oos"]["train_R"] == b.arms["RULES"].diagnostics["train_vs_oos"]["train_R"]
    c69 = next(c for c in a.candidates if c.symbol == "S069")                                     # negative control: without the purge this TRAIN outcome WOULD move
    ta, _ = SIM.simulate(c69, ds_a, arm="RULES", segment="TRAIN", entry_policy="NEXT_BAR_OPEN", friction=DEFAULT_FRICTION, sizing=DEFAULT_SIZING)
    tb, _ = SIM.simulate(c69, ds_b, arm="RULES", segment="TRAIN", entry_policy="NEXT_BAR_OPEN", friction=DEFAULT_FRICTION, sizing=DEFAULT_SIZING)
    assert ta.exit_date > a.train_end and (ta.exit_reason, ta.net_pnl) != (tb.exit_reason, tb.net_pnl)


def test_B04_a_purged_trade_contributes_to_no_train_or_oos_metric():
    ds, ad = crossing()
    purged = run(ds, ad)
    without = run(ds, F.FixtureAdapter(ad.plan, params={"keep": "except:69"}))                    # the same bars, the candidate never generated
    for seg in ("TRAIN", "OOS"):
        p, w = purged.arms["RULES"].metrics[seg], without.arms["RULES"].metrics[seg]
        assert {k: p[k] for k in METRIC_KEYS} == {k: w[k] for k in METRIC_KEYS}, seg               # P/L, R, t-stat, Sharpe, drawdown: as if it never existed
    assert purged.arms["RULES"].metrics["TRAIN"]["candidate_count"] == without.arms["RULES"].metrics["TRAIN"]["candidate_count"] + 1   # it still existed


def test_B05_the_purge_is_exposed_in_the_record_the_api_view_and_the_audit_justifies_it():
    ds, ad = crossing()
    r = run(ds, ad)
    d = SV.detail(rec(r), r.strategy_id)
    assert d["boundary_purged_count"] == 1 and d["run"]["boundary"]["purged"]["RULES"] == r.boundary["purged"]["RULES"]
    assert d["run"]["arms"]["RULES"]["TRAIN"]["boundary_purged_count"] == 1 and d["integrity_status"] == "PASS"
    assert d["run"]["integrity"]["same_candidate_set"] is True and d["run"]["integrity_gate"]["status"] == "PASS"   # two different blocks, both present
    assert r.audit.status == "PASS" and not [v for v in r.audit.violations if v["check"] == "UNJUSTIFIED_PURGE"]


def _forge_purge(d, victim):
    d["arms"]["RULES"]["trades"] = [t for t in d["arms"]["RULES"]["trades"] if t["candidate_id"] != victim["candidate_id"]]
    d["arms"]["RULES"]["simulation_skips"].append({"symbol": victim["symbol"], "signal_date": victim["signal_date"], "candidate_id": victim["candidate_id"],
                                                    "reason": PURGE_REASON, "detail": "forged"})


def test_B06_a_purge_can_never_hide_a_trade_that_resolved_inside_train():
    ds, ad = crossing()
    r = run(ds, ad)
    bars = {s: [b.model_dump() for b in rows] for s, rows in ds.bars.items()}
    d = rec(r)
    victim = next(t for t in d["arms"]["RULES"]["trades"] if t["segment"] == "TRAIN" and t["net_pnl"] < 0)   # a losing TRAIN trade, "purged" to hide it
    _forge_purge(d, victim)
    out = run_audit(d, d["candidates"], [t for a in d["arms"].values() for t in a["trades"]], bars)
    assert out["status"] == "FAIL" and any(v["check"] == "UNJUSTIFIED_PURGE" and v["candidate_id"] == victim["candidate_id"] for v in out["violations"])
    assert any(v["check"] == "UNJUSTIFIED_PURGE" and "holding window ends on or before the TRAIN end" in v["detail"] for v in out["violations"])
    out_w = F.pattern(100, "WWWLL")
    out_w[69] = "W"                                                                               # the boundary candidate now RESOLVES inside TRAIN (target on the TRAIN end) ...
    ds_w, plan_w = F.build(out_w)
    rw = run(ds_w, F.FixtureAdapter(plan_w))
    assert rw.boundary["train_boundary_purged_count"] == 1                                        # ... and is purged all the same: the rule never looks at the outcome
    full, _ = SIM.simulate(next(c for c in rw.candidates if c.symbol == "S069"), ds_w, arm="RULES", segment="TRAIN", entry_policy="NEXT_BAR_OPEN",
                           friction=DEFAULT_FRICTION, sizing=DEFAULT_SIZING)
    assert full.exit_reason == "TARGET" and full.exit_date <= rw.train_end                        # a winner near the boundary is not kept selectively
    dw = rec(rw)                                                                                  # the converse: a crossing candidate kept as a trade is a violation
    skip = next(s for s in dw["arms"]["RULES"]["simulation_skips"] if s["reason"] == PURGE_REASON)
    dw["arms"]["RULES"]["simulation_skips"].remove(skip)
    dw["arms"]["RULES"]["trades"].append({**json.loads(full.model_dump_json()), "arm": "RULES", "trade_id": "k" * 24})
    bars_w = {s: [b.model_dump() for b in rows] for s, rows in ds_w.bars.items()}
    out_w2 = run_audit(dw, dw["candidates"], [t for a in dw["arms"].values() for t in a["trades"]], bars_w)
    assert any(v["check"] == "BOUNDARY_NOT_PURGED" for v in out_w2["violations"])
    d2 = rec(r)
    t0 = next(t for t in d2["arms"]["RULES"]["trades"] if t["segment"] == "TRAIN")
    t0["exit_date"] = d2["oos_start"]                                                            # a TRAIN trade claiming an OOS exit
    out2 = run_audit(d2, d2["candidates"], [t for a in d2["arms"].values() for t in a["trades"]], bars)
    assert "TRAIN_USES_OOS" in [v["check"] for v in out2["violations"]]


def test_B08_the_train_end_is_always_a_point_in_time_cut_of_the_causality_check():
    ds, ad = crossing()
    r = run(ds, ad)
    t = {c["check"]: c for c in r.causality.automated_checks}["RUNTIME_TRUNCATION_INVARIANCE"]
    assert t["result"] == "PASS" and r.train_end in t["detail"]


def test_B07_market_on_close_entries_and_the_sensitivity_neighbours_use_the_same_boundary_policy():
    ds, ad = crossing()
    ad.entry_policy, ad.moc_declared = "SIGNAL_CLOSE_MOC", True
    r = run(ds, ad)
    assert r.boundary["train_boundary_purged_count"] == 1 and r.audit.status == "PASS" and r.eligibility["status"] == "ELIGIBLE"
    ds2, ad2 = crossing()
    s = run(ds2, ad2, neighbours=[{}], adapter_factory=ad2.with_params)                          # the neighbour = the base configuration
    assert s.diagnostics["sensitivity"]["neighbours"][0]["expectancy_R"] == s.arms["RULES"].metrics["ALL"]["expectancy_R"]   # purged there too


# ================================================================ A accounting =====================================================================

def test_A01_accounting_reconciles_every_arm_and_segment():
    ds, ad = crossing()
    r = run(ds, ad, gates=[F.SymbolGate()])
    acc = r.accounting
    assert acc["status"] == "PASS" and acc["arms"]["MODEL"]["status"] == "NOT_APPLICABLE"
    g = acc["arms"]["GATED"]["counts"]
    assert g["candidates"] == g["accepted"] + g["stand_aside"] == 100 and g["accepted"] == g["simulated"] + g["boundary_purged"] + g["skipped"]
    assert acc["arms"]["RULES"]["segments"]["TRAIN"] == {"accepted": 70, "simulated": 69, "boundary_purged": 1, "skipped": 0}
    assert acc["generated"]["candidates"] == 100 and acc["generated"]["missing_data_skipped"] == 0
    ds_m, ad_m = F.fixture("GOOD", missing=(0, 1, 2))
    r2 = E.run_validation(ad_m, ds_m, created_at=AT)
    assert r2.accounting["generated"]["missing_data_skipped"] == 3 and r2.accounting["generated"]["candidates"] == 97 and r2.accounting["status"] == "PASS"


def test_A02_an_accounting_mismatch_is_validation_invalid_and_blocked(monkeypatch):
    real = E._simulate_arm

    def lossy(arm, *a, **k):                                                                       # a simulated bug: one trade silently lost
        real(arm, *a, **k)
        if arm.arm == "RULES":
            arm.trades = arm.trades[1:]
    monkeypatch.setattr(E, "_simulate_arm", lossy)
    ds, ad = crossing()
    r = run(ds, ad)
    assert r.accounting["status"] == "FAIL" and r.verdict["verdict"] == "INVALID" and "ACCOUNTING_MISMATCH" in [x["code"] for x in r.verdict["reasons"]]
    assert r.eligibility["status"] == "BLOCKED" and {"ACCOUNTING", "PERFORMANCE_VERDICT"} <= {x["code"] for x in r.eligibility["reasons"]}
    arm = {"input_candidate_ids": ["a", "b"], "decisions": [{"candidate_id": "a", "decision": "TAKE"}, {"candidate_id": "b", "decision": "TAKE"}],
           "trades": [{"candidate_id": "a"}, {"candidate_id": "a"}], "simulation_skips": [{"candidate_id": "b", "reason": PURGE_REASON}]}
    out = IG.reconcile_arm(arm, {"a": "TRAIN", "b": "OOS"})
    assert out["status"] == "FAIL" and any("more than one simulated trade" in p for p in out["problems"]) and any("OOS candidate was boundary-purged" in p for p in out["problems"])


# ================================================================ G the persistence gate ===========================================================

def _gate(r):
    return IG.evaluate(rec(r))


def test_G01_a_clean_run_is_eligible_and_every_check_is_explained():
    ds, ad = crossing()
    el = _gate(run(ds, ad))
    assert el.status == "ELIGIBLE" and el.integrity_status == "PASS" and el.reasons == []
    assert [c.check for c in el.checks] == ["STRUCTURE", "STRATEGY_VERSION", "PROVENANCE", "CANDIDATE_SET", "ARM_CONTRACT", "CAUSALITY_AUTOMATED", "CAUSALITY_MANUAL_REVIEW",
                                            "REPLAY_PARITY", "MECHANICAL_AUDIT", "ACCOUNTING", "BOUNDARY", "PERFORMANCE_VERDICT"]
    by = {c.check: c for c in el.checks}
    assert by["REPLAY_PARITY"].status == "NOT_APPLICABLE" and "research-only" in by["REPLAY_PARITY"].detail
    assert "PENDING" in by["CAUSALITY_MANUAL_REVIEW"].detail and "never counted as reviewed" in by["CAUSALITY_MANUAL_REVIEW"].detail
    assert "not required by the adapter: corporate_actions, universe, point_in_time" in by["PROVENANCE"].detail          # an explicit decision, recorded


def test_G02_missing_provenance_blocks_persistence_but_the_run_still_executes():
    ds, ad = crossing()
    bare = Dataset(ds.bars, kind=ds.kind, source=ds.source)                                        # no declared provenance at all
    r = run(bare, ad)
    assert r.verdict["verdict"] == "PASS" and r.arms["RULES"].metrics["ALL"]["trade_count"] == 99    # the ephemeral run is complete
    assert r.eligibility["status"] == "BLOCKED" and [x["code"] for x in r.eligibility["reasons"]] == ["PROVENANCE"]
    for k in ("source missing", "retrieved_at missing", "bar_frequency missing", "adjustment missing", "missing_data_policy missing"):
        assert k in r.eligibility["reasons"][0]["text"], k
    part = Dataset(ds.bars, kind=ds.kind, source=ds.source, provenance={**F.PROVENANCE, "missing_data_policy": "", "bar_frequency": "1Min", "retrieved_at": "last week"})
    t = run(part, ad).eligibility["reasons"][0]["text"]
    assert "missing_data_policy missing" in t and "bar_frequency 1Min" in t and "retrieved_at 'last week' is not an ISO date" in t
    ad.provenance_requires = ("universe",)                                                         # the adapter requires a conditional field
    assert "universe missing (required by the adapter)" in run(ds, ad).eligibility["reasons"][0]["text"]
    ds_u = Dataset(ds.bars, kind=ds.kind, source=ds.source, provenance={**F.PROVENANCE, "universe": "fixed fixture symbol set S000-S099"})
    assert run(ds_u, ad).eligibility["status"] == "ELIGIBLE"
    ad.provenance_requires = None                                                                  # undeclared: the decision was never made
    assert "did not declare which conditional provenance" in run(ds_u, ad).eligibility["reasons"][0]["text"]


def test_G03_a_critical_causality_finding_blocks_and_the_manual_review_is_never_assumed():
    ds_l, ad_l = F.fixture("LOOKAHEAD")
    look = E.run_validation(ad_l, ds_l, created_at=AT)
    assert look.eligibility["status"] == "BLOCKED" and "CAUSALITY_AUTOMATED" in [x["code"] for x in look.eligibility["reasons"]]
    assert look.arms["RULES"].metrics["ALL"]["expectancy_R"] > 0.5                                 # it LOOKS profitable - still never persistable
    ds, ad = crossing()
    ad.causality_review = None
    r = run(ds, ad)
    assert r.causality.manual_review_status == "UNDECLARED" and r.eligibility["reasons"][0]["code"] == "CAUSALITY_MANUAL_REVIEW"
    ad.causality_review = {"status": "COMPLETED", "reference": ""}
    assert "COMPLETED without a review reference" in run(ds, ad).eligibility["reasons"][0]["text"]
    ad.causality_review = {"status": "COMPLETED", "reference": "review 2026-10-25 by the research owner"}
    ok = run(ds, ad)
    assert ok.eligibility["status"] == "ELIGIBLE" and ok.causality.manual_review_required is True and ok.causality.manual_review_status == "COMPLETED"
    ad.causality_review = {"status": "PENDING", "reference": ""}
    pend = run(ds, ad)
    assert pend.eligibility["status"] == "ELIGIBLE" and pend.causality.manual_review_status == "PENDING"     # persisted as PENDING, not as reviewed


def test_G04_replay_parity_requirements():
    ds, ad = crossing()
    ad.replay_parity_requirement, ad.replay_parity_status, ad.live_implementation = "EXACT_REQUIRED", "NOT_ESTABLISHED", "setups/engine.py"
    r = run(ds, ad)
    assert r.eligibility["status"] == "BLOCKED" and r.eligibility["reasons"][0]["code"] == "REPLAY_PARITY" and "NOT_ESTABLISHED" in r.eligibility["reasons"][0]["text"]
    ad.replay_parity_status = "ESTABLISHED"
    assert "without a reference to the parity tests" in run(ds, ad).eligibility["reasons"][0]["text"]   # a bare claim is not evidence
    ad.replay_parity_reference = "tests/test_parity_example.py (live evaluator vs replay on equivalent states)"
    assert run(ds, ad).eligibility["status"] == "ELIGIBLE"
    ad.replay_parity_reference = None
    ad.replay_parity_requirement, ad.replay_parity_status = "NOT_APPLICABLE", None                 # a live implementation cannot be declared "not applicable"
    assert "exact replay parity is required" in run(ds, ad).eligibility["reasons"][0]["text"]
    ad.live_implementation = None
    assert run(ds, ad).eligibility["status"] == "ELIGIBLE"                                        # research-only: no live parity demanded
    from strategy_registry.models import StrategyEntry
    from strategy_registry import registry as R
    live_entry = StrategyEntry(**{**R.get("DOWNSIDE_DISLOCATION_RECOVERY").model_dump(), "strategy_id": "FIXTURE_GOOD"})   # the registry knows a live strategy
    rr = E.run_validation(ad, ds, train_ratio=RATIO, created_at=AT, registry={"FIXTURE_GOOD": live_entry})
    assert rr.replay_parity["registry_live"] is True and "exact replay parity is required" in rr.eligibility["reasons"][0]["text"]
    ad.replay_parity_requirement = None
    assert "did not declare its replay-parity requirement" in run(ds, ad).eligibility["reasons"][0]["text"]


def test_G05_audit_failure_blocks_persistence():
    ds, ad = crossing()
    d = rec(run(ds, ad))
    d["audit"]["status"], d["audit"]["violations"] = "FAIL", [{"check": "STOP_CONSISTENCY"}]
    el = IG.evaluate(d)
    assert el.status == "BLOCKED" and [x["code"] for x in el.reasons] == ["MECHANICAL_AUDIT"]
    d2 = rec(run(ds, ad))
    d2["audit"]["checked"] = d2["audit"]["checked"] - 1                                             # an incomplete audit
    assert IG.evaluate(d2).reasons[0]["code"] == "MECHANICAL_AUDIT"


def test_G06_integrity_is_separate_from_the_performance_verdict():
    for name, verdict in (("OVERFIT", "FAIL"), ("TOO_SMALL", "INSUFFICIENT_DATA"), ("FRICTION_KILLED", "FAIL"), ("AMBIGUOUS", "MIXED"), ("GOOD", "PASS")):
        ds, ad = F.fixture(name)
        r = E.run_validation(ad, ds, created_at=AT)
        assert r.verdict["verdict"] == verdict and r.eligibility["integrity_status"] == "PASS" and r.eligibility["status"] == "ELIGIBLE", name
    ds, ad = F.fixture("CANDIDATE_MISMATCH")
    bad = E.run_validation(ad, ds, created_at=AT, arm_hooks={"GATED": F.drop_first})
    assert bad.verdict["verdict"] == "INVALID" and bad.eligibility["integrity_status"] == "FAIL" and "CANDIDATE_SET" in [x["code"] for x in bad.eligibility["reasons"]]


# ================================================================ S the store ======================================================================

def test_S01_performance_fail_and_insufficient_data_with_integrity_pass_are_persisted(tmp_path, monkeypatch):
    from strategy_validation.store import StrategyValidationStore
    monkeypatch.setenv("STOCK_AGENT_ENV", "test")
    s = StrategyValidationStore(tmp_path / "v.db")
    for name in ("OVERFIT", "TOO_SMALL"):
        ds, ad = F.fixture(name)
        r = E.run_validation(ad, ds, created_at=AT)
        s.insert(r)
        got = s.run(r.run_id)
        assert got["verdict"]["verdict"] in ("FAIL", "INSUFFICIENT_DATA") and got["eligibility"]["status"] == "ELIGIBLE" and got["accounting"]["status"] == "PASS"
        assert SV.summary(got, r.strategy_id)["integrity_status"] == "PASS" and SV.summary(got, r.strategy_id)["boundary_purged_count"] == 0


def test_S02_the_store_enforces_the_gate_itself_and_ignores_a_forged_eligibility(tmp_path, monkeypatch):
    from strategy_validation.store import PersistenceBlocked, StrategyValidationStore
    monkeypatch.setenv("STOCK_AGENT_ENV", "test")
    s = StrategyValidationStore(tmp_path / "v.db")
    ds, ad = crossing()
    bare = run(Dataset(ds.bars, kind=ds.kind, source=ds.source), ad)
    bare.eligibility = {"status": "ELIGIBLE", "integrity_status": "PASS", "checks": [], "reasons": []}   # forged: the store recomputes
    with pytest.raises(PersistenceBlocked) as exc:
        s.insert(bare)
    assert exc.value.eligibility.reasons[0]["code"] == "PROVENANCE" and s.runs() == []
    ds_l, ad_l = F.fixture("LOOKAHEAD")
    look = E.run_validation(ad_l, ds_l, created_at=AT)
    with pytest.raises(PersistenceBlocked):
        s.insert(look)
    assert s.runs() == []                                                                          # nothing was written


def test_S03_stage_62_records_stay_readable_and_are_never_back_filled(tmp_path, monkeypatch):
    from database.strategy_validation_migrations import run_strategy_validation_migrations
    from strategy_validation.store import StrategyValidationStore
    monkeypatch.setenv("STOCK_AGENT_ENV", "test")
    ds, ad = F.fixture("GOOD")
    r = E.run_validation(ad, ds, created_at=AT)
    d = rec(r)
    for k in ("provenance", "boundary", "accounting", "replay_parity", "eligibility"):
        d.pop(k)                                                                                   # exactly what a validation_v1 record looked like
    d["validation_version"] = "validation_v1"
    d["causality"].pop("manual_review_status"), d["causality"].pop("manual_review_reference")
    cands = d.pop("candidates")
    trades = [t for a in d["arms"].values() for t in a.pop("trades")]
    db = tmp_path / "v.db"
    with sqlite3.connect(str(db)) as conn:
        run_strategy_validation_migrations(conn)
        conn.execute("INSERT INTO strategy_validation_runs (run_id, strategy_id, strategy_version, dataset_id, dataset_kind, verdict, validation_version, candidate_count, created_at, run_json) "
                     "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", (r.run_id, r.strategy_id, r.strategy_version, r.dataset_id, r.dataset_kind, "PASS", "validation_v1", r.candidate_count, AT, json.dumps(d)))
        conn.executemany("INSERT INTO strategy_validation_candidates (run_id, candidate_id, signal_date, symbol, candidate_json) VALUES (?, ?, ?, ?, ?)",
                         [(r.run_id, c["candidate_id"], c["signal_date"], c["symbol"], json.dumps(c)) for c in cands])
        conn.executemany("INSERT INTO strategy_validation_trades (run_id, trade_id, arm, candidate_id, segment, trade_json) VALUES (?, ?, ?, ?, ?, ?)",
                         [(r.run_id, t["trade_id"], t["arm"], t["candidate_id"], t["segment"], json.dumps(t)) for t in trades])
    s = StrategyValidationStore(db)
    old = s.latest("FIXTURE_GOOD")
    assert old["validation_version"] == "validation_v1" and len(s.trades(r.run_id)) == 200 and len(s.candidates(r.run_id)) == 100
    summ, det = SV.summary(old, "FIXTURE_GOOD"), SV.detail(old, "FIXTURE_GOOD")
    assert summ["validation_status"] == "PASS" and summ["integrity_recorded"] is False
    assert (summ["integrity_status"], summ["persistence_eligibility"], summ["boundary_purged_count"], summ["accounting_status"]) == (None, None, None, None)
    assert det["run"]["integrity_gate"] == {"recorded": False, "note": "not recorded: a Stage 6.2 run (validation_v1) predates the integrity gate"}
    assert det["run"]["integrity"]["same_candidate_set"] is True                                   # the candidate-set integrity is not shadowed
    assert det["run"]["boundary"] is None and det["run"]["accounting"] is None
    s.insert(E.run_validation(ad, ds, created_at=AT))                                             # a v2 run of the same strategy version is accepted beside it
    assert [x["validation_version"] for x in s.runs("FIXTURE_GOOD")].count("validation_v1") == 1 and json.loads(sqlite3.connect(str(db)).execute(
        "SELECT run_json FROM strategy_validation_runs WHERE run_id = ?", (r.run_id,)).fetchone()[0]) == d       # the old row is untouched


# ================================================================ C the CLI ========================================================================

def test_C01_persist_stores_only_an_eligible_run_and_a_blocked_one_exits_3(tmp_path, monkeypatch, capsys):
    from strategy_validation import cli
    from strategy_validation.store import StrategyValidationStore
    monkeypatch.setenv("STOCK_AGENT_ENV", "test")
    ds, ad = crossing()
    dump(Dataset(ds.bars, kind=ds.kind, source=ds.source), str(tmp_path / "bare.json"))          # no provenance in the file
    db = str(tmp_path / "v.db")
    code = cli.main(["validate", "--strategy", "FIXTURE_GOOD", "--dataset", str(tmp_path / "bare.json"), "--train-ratio", str(RATIO), "--persist", "--db", db], adapters={"FIXTURE_GOOD": ad})
    out = capsys.readouterr().out
    assert code == 3 and "persistence BLOCKED" in out and "PROVENANCE" in out and "stored run" not in out and StrategyValidationStore(Path(db)).runs() == []
    (tmp_path / "prov.json").write_text(json.dumps(F.PROVENANCE), encoding="utf-8")
    code = cli.main(["validate", "--strategy", "FIXTURE_GOOD", "--dataset", str(tmp_path / "bare.json"), "--train-ratio", str(RATIO), "--persist", "--db", db,
                     "--provenance", str(tmp_path / "prov.json")], adapters={"FIXTURE_GOOD": ad})
    out = capsys.readouterr().out
    assert code == 0 and "integrity: PASS - persistence ELIGIBLE" in out and "1 TRAIN outcome(s) purged" in out and "stored run" in out
    assert len(StrategyValidationStore(Path(db)).runs()) == 1
    (tmp_path / "bad.json").write_text("[1, 2]", encoding="utf-8")
    assert cli.main(["validate", "--strategy", "FIXTURE_GOOD", "--dataset", str(tmp_path / "bare.json"), "--provenance", str(tmp_path / "bad.json")],
                    adapters={"FIXTURE_GOOD": ad}) == 1


def test_C02_an_ephemeral_run_still_works_for_debugging(tmp_path, capsys):
    from strategy_validation import cli
    ds, ad = crossing()
    dump(Dataset(ds.bars, kind=ds.kind, source=ds.source), str(tmp_path / "bare.json"))
    code = cli.main(["validate", "--strategy", "FIXTURE_GOOD", "--dataset", str(tmp_path / "bare.json"), "--train-ratio", str(RATIO), "--output", str(tmp_path / "run.json")],
                    adapters={"FIXTURE_GOOD": ad})
    out = capsys.readouterr().out
    assert code == 0 and "integrity: FAIL - persistence BLOCKED" in out and "ephemeral run (not stored" in out
    j = json.loads((tmp_path / "run.json").read_text(encoding="utf-8"))
    assert j["verdict"]["verdict"] == "PASS" and j["eligibility"]["status"] == "BLOCKED" and j["arms"]["RULES"]["metrics"]["TRAIN"]["boundary_purged_count"] == 1


# ================================================================ D Downside Recovery / isolation ==================================================

def test_D01_downside_recovery_stays_not_run():
    from strategy_registry import registry as R
    from strategy_registry import service as SR
    from strategy_validation import cli
    from strategy_validation.adapters import ADAPTERS, DownsideRecoveryAdapter, adapter_for
    a = DownsideRecoveryAdapter()
    assert a.historical_replay_supported is False and (a.replay_parity_requirement, a.replay_parity_status) == ("EXACT_REQUIRED", "NOT_ESTABLISHED")
    assert adapter_for("DOWNSIDE_DISLOCATION_RECOVERY", R.get("DOWNSIDE_DISLOCATION_RECOVERY"))[0] is None and sorted(ADAPTERS) == ["DOWNSIDE_DISLOCATION_RECOVERY", "VALUE_QUALITY"]
    with pytest.raises(E.ValidationError):
        E.run_validation(a, F.fixture("GOOD")[0])
    v = SR.detail_view("DOWNSIDE_DISLOCATION_RECOVERY")["validation"]
    assert v["validation_status"] == "NOT_RUN" and v["run"] is None and v["integrity_recorded"] is False and v["integrity_status"] is None   # nothing fabricated
    assert cli.main(["validate", "--strategy", "DOWNSIDE_DISLOCATION_RECOVERY", "--dataset", "x.csv"]) == 2


def test_D02_no_broker_execution_or_network_code_in_the_new_module():
    src = (ROOT / "strategy_validation" / "integrity.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    mods = {n.module or "" for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)} | {a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
    assert all(m.startswith(("strategy_validation", "pydantic", "__future__", "typing", "hashlib", "re", "datetime")) for m in mods), mods
    for word in ("broker", "alpaca", "robinhood", "requests", "urllib", "socket", "submit", "enable_auto", "sqlite3", "insert into", "paper"):
        assert word not in src.lower(), word                                                      # the gate only reads a record; it writes nothing itself


def test_D03_the_route_module_stays_get_only():
    src = (ROOT / "api" / "routes" / "strategy_validation.py").read_text(encoding="utf-8")
    decos = [ast.unparse(d.func) if isinstance(d, ast.Call) else ast.unparse(d) for n in ast.walk(ast.parse(src)) if isinstance(n, ast.FunctionDef) for d in n.decorator_list]
    assert decos and all(d == "router.get" for d in decos) and "run_validation" not in src and ".insert(" not in src


def test_D04_node_tests_of_the_integrity_display():
    r = subprocess.run(["node", "--test", str(ROOT / "tests" / "frontend" / "strategy_validation.test.cjs")], capture_output=True, text=True, cwd=ROOT)
    assert r.returncode == 0 and "fail 0" in r.stdout and "pass 11" in r.stdout, r.stdout[-2000:]
