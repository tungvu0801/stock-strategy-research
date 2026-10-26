"""Stage 6.4.1 — cross-sectional validation (validation_v3): strategy_validation/cross_sectional.py, the SELECTION_CONTROL integrity check, the
methodology evaluations store, the CLI `evaluate`, the Strategy Lab payload (DESIGN_641_CROSS_SECTIONAL_VALIDATION.md).

  A-D  one observation per rebalance period; strategy / control alignment; the period excess; the excess t-statistic over periods
  E-G  too few OOS periods; positive absolute return without control excess; beating SPY without control excess -> never ESTABLISHED
  H-J  a control mismatch is detected; the boundary month is never one-sided; concentration
  K-L  an older stored run is assessed by a separate methodology evaluation and never rewritten; old records stay readable
  M-N  event-driven strategies are unaffected; no broker / execution import
  O-Z  the integrity gate, the store, the policy table, the CLI, the read service, pre-registration, documents

Fixtures are SYNTHETIC (tests/value_quality_fixtures.py, tests/strategy_validation_fixtures.py). The real Value + Quality run is evaluated by
`python -m strategy_validation evaluate --run ... --dataset runtime/vq/vq_bundle.json`; test_K02 checks it only where that local data exists."""
from __future__ import annotations

import ast
import copy
import functools
import hashlib
import io
import json
import math
import re
import sqlite3
import statistics
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

import strategy_validation_fixtures as FX  # noqa: E402
import value_quality_fixtures as F  # noqa: E402
from strategy_registry import registry as R  # noqa: E402
from strategy_validation import (CS_CONCENTRATION_SHARE, CS_MIN_OOS_PERIODS, CS_MIN_PERIODS_T, CS_MIN_TOTAL_PERIODS, CS_T_ESTABLISHED,  # noqa: E402
                                 CS_T_NEGLIGIBLE, CS_TRAIN_CONTRADICTION_T, EDGE_STATUSES, VERSION)
from strategy_validation import cross_sectional as CS  # noqa: E402
from strategy_validation import engine as E  # noqa: E402
from strategy_validation import integrity as IG  # noqa: E402
from value_quality.adapter import ValueQualityAdapter  # noqa: E402

AT = "2026-10-25T00:00:00+00:00"
FROZEN_HASH = "b5d4a56ef5bba61f8250b3cd25182cc1655738adf0605a77671fd029883f8530"
LONG_END = "2022-12-30"                                                                         # >= 12 OOS monthly periods


def now_text(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8").replace("\r\n", "\n")


@functools.lru_cache(maxsize=None)
def long_run():
    ds = F.dataset(F.run_set(), end=LONG_END)
    return ds, E.run_validation(ValueQualityAdapter({"top_n": 5}), ds, registry=R.by_id(), created_at=AT)


@functools.lru_cache(maxsize=None)
def short_run():
    ds = F.dataset(F.run_set())
    return ds, E.run_validation(ValueQualityAdapter({"top_n": 5}), ds, registry=R.by_id(), created_at=AT)


def rec_of(run) -> dict:
    return json.loads(run.model_dump_json())


def table(run, ds, **over):
    """The full period table (with the per-position detail) of a run, rebuilt from its record."""
    rec = rec_of(run)
    ad = ValueQualityAdapter({"top_n": 5})
    snaps = over.pop("snapshots", None) or ad.selection_snapshots(ds)
    return rec, snaps, CS.period_table(rec, rec["arms"]["RULES"]["trades"], rec["candidates"], ds, snaps)


def rows_of(ex, base=0.01):
    """Synthetic INCLUDED period rows with the given excesses (strategy = base + excess, control = base)."""
    out = []
    for i, x in enumerate(ex):
        out.append({"rebalance": f"2020-{(i % 12) + 1:02d}-01", "status": "INCLUDED", "strategy": {"net": base + x, "gross": base + x, "entries": 1, "positions": 5},
                    "control": {"net": base, "gross": base, "entries": 1, "positions": 50}, "benchmark": {"return": -0.02}, "excess": x, "excess_gross": x})
    return out


PARITY_OK = {"status": "PASS", "checks": [], "failed": []}


# ================================================================ A-D periods ======================================================================

def test_A01_the_legs_of_one_rebalance_are_one_period_observation():
    ds, run = long_run()
    cs = run.cross_sectional
    inc = [r for r in cs["periods"] if r["status"] == "INCLUDED"]
    assert inc and all(r["strategy"]["positions"] == 5 for r in inc)                             # five legs ...
    legs = sum(r["strategy"]["positions"] for r in inc)
    assert cs["segments"]["ALL"]["period_count"] == len(inc) == legs // 5                          # ... one observation
    assert cs["segments"]["TRAIN"]["period_count"] + cs["segments"]["OOS"]["period_count"] == len(inc)
    assert run.arms["RULES"].metrics["ALL"]["trade_count"] > 4 * len(inc)                          # the per-leg sample is ~5x larger: not the evidence unit


def test_B01_strategy_and_control_periods_align_on_the_same_sessions_and_prices():
    ds, run = long_run()
    rec, snaps, rows = table(run, ds)
    assert [r["rebalance"] for r in rows] == [s["rebalance"] for s in snaps]
    for r in rows:
        if r["status"] != "INCLUDED":
            continue
        ctrl = {x["symbol"]: x for x in r["control"]["positions_detail"]}
        assert {x["symbol"] for x in r["strategy"]["legs_detail"]} <= set(ctrl)                  # the selection is within the control's members
        for x in r["strategy"]["legs_detail"]:
            c = ctrl[x["symbol"]]
            assert (c["entry_date"], c["exit_date"], c["entry_raw"], c["exit_raw"]) == (x["entry_date"], x["exit_date"], x["entry_raw"], x["exit_raw"])
            assert c["entry_trade"] <= x["entry_trade"] and c["exit_trade"] <= x["exit_trade"]    # the control already holds what the strategy buys / keeps


def test_C01_the_period_excess_is_strategy_minus_control_with_equal_capital_on_both_sides():
    ds, run = long_run()
    rec, snaps, rows = table(run, ds)
    r = next(r for r in rows if r["status"] == "INCLUDED" and r["segment"] == "OOS")
    trades = {t["candidate_id"]: t for t in rec["arms"]["RULES"]["trades"]}
    legs = [x for x in r["strategy"]["legs_detail"]]
    assert r["strategy"]["net"] == pytest.approx(statistics.mean(trades[x["candidate_id"]]["trade_return"] for x in legs), abs=1e-9)
    assert r["control"]["net"] == pytest.approx(statistics.mean(x["net"] for x in r["control"]["positions_detail"]))
    assert r["excess"] == pytest.approx(r["strategy"]["net"] - r["control"]["net"])
    slip = rec["friction_model"]["slippage_bps_per_side"] / 10000
    p = r["control"]["positions_detail"][0]
    ef = p["entry_raw"] * (1 + slip) if p["entry_trade"] else p["entry_raw"]
    xf = p["exit_raw"] * (1 - slip) if p["exit_trade"] else p["exit_raw"]
    assert p["net"] == pytest.approx(xf / ef - 1) and p["gross"] == pytest.approx(p["exit_raw"] / p["entry_raw"] - 1)   # the run's friction, real orders only


def test_D01_the_excess_t_statistic_uses_one_excess_per_period_and_the_sample_stdev():
    ds, run = long_run()
    inc = [r for r in run.cross_sectional["periods"] if r["status"] == "INCLUDED" and r["segment"] == "OOS"]
    ex = [r["excess"] for r in inc]
    want = statistics.mean(ex) / (statistics.stdev(ex) / math.sqrt(len(ex)))
    assert run.cross_sectional["segments"]["OOS"]["excess_t_stat"] == pytest.approx(want, abs=1e-4)
    few = CS.segment_stats(rows_of([0.01, -0.002, 0.004, 0.003, 0.0]), 12.0)                       # 5 < CS_MIN_PERIODS_T
    assert few["excess_t_stat"] is None and "fewer than 6 periods" in few["excess_t_note"] and CS_MIN_PERIODS_T == 6
    s = CS.segment_stats(rows_of([0.01, -0.002, 0.004, 0.003, 0.0, 0.006]), 12.0)
    assert s["excess_t_stat"] == pytest.approx(statistics.mean([0.01, -0.002, 0.004, 0.003, 0.0, 0.006]) / (statistics.stdev([0.01, -0.002, 0.004, 0.003, 0.0, 0.006]) / math.sqrt(6)), abs=1e-4)


# ================================================================ E-G the edge is never inferred from absolute returns ==========================

def test_E01_too_few_oos_periods_is_insufficient_data_for_the_selection_edge():
    ds, run = short_run()
    assert run.cross_sectional["segments"]["OOS"]["period_count"] < CS_MIN_OOS_PERIODS == 12
    assert run.selection_edge["status"] == "INSUFFICIENT_DATA" and run.selection_edge["reasons"][0]["code"] == "TOO_FEW_OOS_PERIODS"
    assert run.performance_verdict["verdict"] == "PASS" and run.verdict["verdict"] == "INSUFFICIENT_DATA" and run.verdict["eligible_for_review"] is False


def test_F01_positive_absolute_return_without_control_excess_is_not_established_and_never_PASS():
    ds, run = long_run()
    oo = run.cross_sectional["segments"]["OOS"]
    assert run.arms["RULES"].metrics["OOS"]["net_pnl"] > 0 and run.arms["RULES"].metrics["ALL"]["mean_R_t_stat"] > 2   # absolute PASS material ...
    assert run.performance_verdict["verdict"] == "PASS" and oo["period_count"] >= CS_MIN_OOS_PERIODS and oo["excess_t_stat"] < CS_T_NEGLIGIBLE
    assert run.selection_edge["status"] == "NOT_ESTABLISHED" and run.selection_edge["reasons"][0]["code"] == "EXCESS_NEGLIGIBLE"
    v = run.verdict
    assert v["verdict"] == "FAIL" and v["eligible_for_review"] is False and v["reasons"][0]["code"] == "SELECTION_EDGE_NOT_ESTABLISHED"
    assert "Absolute performance: PASS" in v["reasons"][0]["text"] and run.arms["RULES"].verdict["verdict"] == "PASS"   # the absolute verdict is kept


def test_G01_beating_spy_is_never_selection_evidence():
    rows = rows_of([0.0005, -0.0004, 0.0003, -0.0002, 0.0001, 0.0, 0.0004, -0.0003, 0.0002, -0.0001, 0.0003, -0.0002])   # ~no excess over the control
    for r in rows:
        r["benchmark"]["return"] = -0.03                                                         # the strategy beats SPY by ~4 % every month
    seg = CS.segment_stats(rows, 12.0)
    assert seg["strategy_total_return"] - seg["benchmark_total_return"] > 0.4
    edge = CS.selection_edge({"TRAIN": seg, "OOS": seg, "ALL": CS.segment_stats(rows + rows, 12.0)}, PARITY_OK)
    assert edge["status"] == "NOT_ESTABLISHED"
    for r in rows:
        r["benchmark"]["return"] = 0.05                                                         # SPY changed: the edge cannot change
    seg2 = CS.segment_stats(rows, 12.0)
    assert CS.selection_edge({"TRAIN": seg2, "OOS": seg2, "ALL": CS.segment_stats(rows + rows, 12.0)}, PARITY_OK)["status"] == "NOT_ESTABLISHED"
    ds, run = long_run()
    assert run.cross_sectional["market_benchmark"]["role"] == "MARKET_BENCHMARK" and run.cross_sectional["control"]["role"] == "SELECTION_CONTROL"
    assert run.cross_sectional["control"]["control_id"] == "EQUAL_WEIGHT_ELIGIBLE_UNIVERSE"


# ================================================================ H-J fairness =====================================================================

def _parity(run, ds, rec=None, snaps=None, trades=None, control=None):
    rec = rec or rec_of(run)
    ad = ValueQualityAdapter({"top_n": 5})
    snaps = snaps if snaps is not None else ad.selection_snapshots(ds)
    trades = trades if trades is not None else rec["arms"]["RULES"]["trades"]
    rows = CS.period_table(rec, trades, rec["candidates"], ds, snaps)
    return CS.parity_audit(rec, rows, snaps, rec["candidates"], trades, control if control is not None else ad.control_definition)


def test_H01_a_control_mismatch_is_detected_and_makes_the_edge_INVALID_not_the_research_run():
    ds, run = long_run()
    assert _parity(run, ds)["status"] == "PASS" and len(_parity(run, ds)["checks"]) == 8
    snaps = copy.deepcopy(ValueQualityAdapter({"top_n": 5}).selection_snapshots(ds))
    k = next(i for i, s in enumerate(snaps) if s["selected"])
    snaps[k]["eligible"] = [x for x in snaps[k]["eligible"] if x != snaps[k]["selected"][0]]     # the control lost a selected stock
    assert "SAME_UNIVERSE_SNAPSHOT" in _parity(run, ds, snaps=snaps)["failed"]
    bad_ctl = {**ValueQualityAdapter().control_definition, "friction": "NONE"}
    assert "SAME_FRICTION" in _parity(run, ds, control=bad_ctl)["failed"]
    rec = rec_of(run)
    rec["config_snapshot"]["sizing"]["mode"] = "RISK"
    assert "SAME_CAPITAL_CONVENTION" in _parity(run, ds, rec=rec)["failed"]
    trades = copy.deepcopy(rec_of(run)["arms"]["RULES"]["trades"])
    t = next(t for t in trades if t["segment"] == "OOS")
    t["exit_date"] = "2099-01-01"
    assert "SAME_PRICE_TIMESTAMPS" in _parity(run, ds, trades=trades)["failed"]
    edge = CS.selection_edge(run.cross_sectional["segments"], {"status": "FAIL", "failed": ["SAME_FRICTION"]})
    assert edge["status"] == "INVALID" and CS.overall_verdict(run.performance_verdict, "INVALID")["verdict"] == "MIXED"   # not INVALID research: MIXED


def test_H02_a_narrowed_control_membership_is_detected_against_the_candidate_set(tmp_path, monkeypatch):
    from strategy_validation.store import StoreError, StrategyValidationStore
    monkeypatch.setenv("STOCK_AGENT_ENV", "test")
    ds, run = long_run()

    class Narrowed(ValueQualityAdapter):                                                           # same id, version and candidates; a cherry-picked control
        def selection_snapshots(self, dataset):
            out = []
            for s in super().selection_snapshots(dataset):
                rest = [x for x in s["eligible"] if x not in s["selected"]]
                out.append({**s, "eligible": s["selected"] + rest[: len(rest) // 3]})
            return out
    au = _parity(run, ds, snaps=Narrowed({"top_n": 5}).selection_snapshots(ds))
    assert "SAME_UNIVERSE_SNAPSHOT" in au["failed"] and "the candidate set records" in {c["check"]: c["detail"] for c in au["checks"]}["SAME_UNIVERSE_SNAPSHOT"]
    old = _as_v2(run)
    db = tmp_path / "v.db"
    _store_raw(db, old)
    store = StrategyValidationStore(db)
    stored = store.run(old["run_id"])
    ev = CS.assess_stored_run(stored, store.candidates(old["run_id"]), store.trades(old["run_id"], "RULES"), ds, Narrowed({"top_n": 5}), created_at=AT)
    assert ev["selection_edge"]["status"] == "INVALID"                                              # the narrowed control is never a selection claim
    ev2 = copy.deepcopy(ev)                                                                         # even with its parity audit forged to PASS
    ev2["evaluation_id"] = "d" * 32
    ev2["cross_sectional"]["parity"] = {**ev2["cross_sectional"]["parity"], "status": "PASS", "failed": [],
                                         "checks": [{**c, "status": "PASS"} for c in ev2["cross_sectional"]["parity"]["checks"]]}
    ev2["selection_edge"] = CS.selection_edge(ev2["cross_sectional"]["segments"], ev2["cross_sectional"]["parity"])
    ev2["verdict"] = CS.overall_verdict(ev2["performance_verdict"], ev2["selection_edge"]["status"])
    with pytest.raises(StoreError, match="the candidate set records"):
        store.insert_evaluation(ev2)
    assert store.evaluations(old["run_id"]) == []


def test_H03_a_control_member_without_an_entry_bar_excludes_the_period_on_both_sides():
    ds, run = long_run()
    rec, snaps, rows = table(run, ds)
    k = next(i for i, r in enumerate(rows) if r["status"] == "INCLUDED" and r["segment"] == "OOS")
    R0 = rows[k]["rebalance"]
    sym = next(x for x in snaps[k]["eligible"] if x not in snaps[k]["selected"])
    cut = F.Dataset({s: ([b for b in bars if b.date <= R0] if s == sym else bars) for s, bars in ds.bars.items()}, kind=ds.kind, aux=ds.aux)
    rows2 = CS.period_table(rec, rec["arms"]["RULES"]["trades"], rec["candidates"], cut, snaps)
    assert rows2[k]["status"] == "EXCLUDED" and rows2[k]["reason"] == "CONTROL_MEMBER_NO_ENTRY_BAR" and rows2[k]["excess"] is None
    assert rows2[k]["control"]["missing_entry"] == [sym]


def test_I01_the_boundary_month_is_excluded_on_both_sides_and_can_never_be_one_sided():
    ds, run = long_run()
    rows = run.cross_sectional["periods"]
    b = [r for r in rows if r["reason"] == "BOUNDARY"]
    assert len(b) == 1 and b[0]["excess"] is None and b[0]["segment"] is None and run.cross_sectional["excluded_periods"]["BOUNDARY"] == 1
    train = [r for r in rows if r["segment"] == "TRAIN"]
    assert all((r["exit_session"] or "") <= run.train_end for r in train) and b[0]["rebalance"] <= run.train_end < b[0]["exit_session"]
    rec, snaps, full = table(run, ds)
    k = next(i for i, r in enumerate(full) if r["reason"] == "BOUNDARY")
    full[k] = {**full[k], "status": "INCLUDED", "reason": "", "segment": "TRAIN", "excess": -full[k]["control"]["net"],
               "strategy": {**full[k]["strategy"], "net": 0.0, "gross": 0.0}}                    # the Stage 6.4 bug: cash on one side only
    au = CS.parity_audit(rec, full, snaps, rec["candidates"], rec["arms"]["RULES"]["trades"], ValueQualityAdapter().control_definition)
    assert "BOUNDARY_BOTH_SIDES" in au["failed"]


def test_J01_concentrated_excess_warns_and_blocks_established():
    c = CS.concentration([0.10, 0.001, -0.002, 0.001, 0.0])
    assert c["concentrated"] and c["top_period_share"] > CS_CONCENTRATION_SHARE and "one period carries" in c["text"]
    assert CS.concentration([0.01, 0.012, 0.009, 0.011])["concentrated"] is False
    assert CS.concentration([0.05, -0.01, -0.01, -0.01, -0.01])["concentrated"] is True           # the excess vanishes without its best period
    strong = [0.012, 0.009, 0.011, 0.01, 0.013, 0.008, 0.01, 0.012, 0.009, 0.011, 0.01, 0.0105]
    seg = CS.segment_stats(rows_of(strong), 12.0)
    assert CS.selection_edge({"TRAIN": seg, "OOS": seg, "ALL": CS.segment_stats(rows_of(strong * 2), 12.0)}, PARITY_OK)["status"] == "ESTABLISHED"
    spiky = [0.30] + [0.0005 * (-1) ** i for i in range(11)]
    seg2 = CS.segment_stats(rows_of(spiky), 12.0)
    e2 = CS.selection_edge({"TRAIN": seg, "OOS": seg2, "ALL": CS.segment_stats(rows_of(strong + spiky), 12.0)}, PARITY_OK)
    assert e2["status"] in ("NOT_ESTABLISHED", "WEAK") and e2["status"] != "ESTABLISHED"


# ================================================================ K-L history is never rewritten ===================================================

def _as_v2(run) -> dict:
    """The run as a validation_v2 record looked (Stage 6.4): no v3 fields, its verdict the absolute one."""
    d = rec_of(run)
    for k in ("evaluation_mode", "performance_verdict", "selection_edge", "cross_sectional"):
        d.pop(k)
    d["verdict"] = rec_of(run)["performance_verdict"]
    d["validation_version"] = "validation_v2"
    return d


def _store_raw(db: Path, d: dict):
    from database.strategy_validation_migrations import run_strategy_validation_migrations
    d = copy.deepcopy(d)
    cands = d.pop("candidates")
    trades = []
    for a in d["arms"].values():
        ts = a.pop("trades")
        a["trade_count"] = len(ts)
        trades += ts
    conn = sqlite3.connect(str(db))
    run_strategy_validation_migrations(conn)
    conn.execute("INSERT INTO strategy_validation_runs (run_id, strategy_id, strategy_version, dataset_id, dataset_kind, verdict, validation_version, candidate_count, "
                 "created_at, run_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", (d["run_id"], d["strategy_id"], d["strategy_version"], d["dataset_id"], d["dataset_kind"],
                                                                                d["verdict"]["verdict"], d["validation_version"], d["candidate_count"], d["created_at"],
                                                                                json.dumps(d, sort_keys=True)))
    conn.executemany("INSERT INTO strategy_validation_candidates (run_id, candidate_id, signal_date, symbol, candidate_json) VALUES (?, ?, ?, ?, ?)",
                     [(d["run_id"], c["candidate_id"], c["signal_date"], c["symbol"], json.dumps(c, sort_keys=True)) for c in cands])
    conn.executemany("INSERT INTO strategy_validation_trades (run_id, trade_id, arm, candidate_id, segment, trade_json) VALUES (?, ?, ?, ?, ?, ?)",
                     [(d["run_id"], t["trade_id"], t["arm"], t["candidate_id"], t["segment"], json.dumps(t, sort_keys=True)) for t in trades])
    conn.commit()
    conn.close()


def _row_hash(db: Path, run_id: str) -> str:
    j = sqlite3.connect(str(db)).execute("SELECT run_json FROM strategy_validation_runs WHERE run_id = ?", (run_id,)).fetchone()[0]
    return hashlib.sha256(j.encode("utf-8")).hexdigest()


def test_K01_an_older_value_quality_run_gets_a_separate_methodology_evaluation_and_is_never_rewritten(tmp_path, monkeypatch):
    from strategy_validation import service as SV
    from strategy_validation.store import StoreError, StrategyValidationStore
    monkeypatch.setenv("STOCK_AGENT_ENV", "test")
    ds, run = long_run()
    old = _as_v2(run)
    db = tmp_path / "v.db"
    _store_raw(db, old)
    before = _row_hash(db, old["run_id"])
    store = StrategyValidationStore(db)
    stored = store.run(old["run_id"])
    assert stored["validation_version"] == "validation_v2" and stored["verdict"]["verdict"] == "PASS" and "selection_edge" not in stored
    ev = CS.assess_stored_run(stored, store.candidates(old["run_id"]), store.trades(old["run_id"], "RULES"), ds, ValueQualityAdapter({"top_n": 5}),
                              registry=R.by_id(), created_at=AT)
    assert ev["candidate_set_reproduced"] and ev["candidate_set_hash"] == old["candidate_set_hash"] and ev["methodology_version"] == VERSION == "validation_v3"
    assert ev["selection_edge"]["status"] == "NOT_ESTABLISHED" and ev["verdict"]["verdict"] == "FAIL" and ev["performance_verdict"] == old["verdict"]
    assert ev["cross_sectional"]["segments"] == run.cross_sectional["segments"]                    # the same assessment the v3 engine makes
    store.insert_evaluation(ev)
    assert _row_hash(db, old["run_id"]) == before                                                  # the run row is byte-identical
    latest = store.latest(old["strategy_id"])
    assert latest["methodology_evaluation"]["evaluation_id"] == ev["evaluation_id"] and latest["verdict"]["verdict"] == "PASS"   # the run's own verdict stays
    s = SV.summary(latest, old["strategy_id"])
    assert (s["validation_status"], s["performance_verdict"], s["selection_edge_status"], s["methodology_version"]) == ("FAIL", "PASS", "NOT_ESTABLISHED", "validation_v3")
    d = SV.detail(latest, old["strategy_id"])
    assert d["run"]["verdict"]["verdict"] == "PASS" and d["overall_verdict"]["verdict"] == "FAIL" and d["methodology_evaluation"]["evaluated_run_validation_version"] == "validation_v2"
    with sqlite3.connect(str(db)) as conn:
        for sql in ("UPDATE strategy_validation_evaluations SET verdict = 'PASS'", "DELETE FROM strategy_validation_evaluations"):
            with pytest.raises(sqlite3.DatabaseError):
                conn.execute(sql)
    bad = {**ev, "evaluation_id": "b" * 32, "candidate_set_hash": "0" * 32}
    with pytest.raises(StoreError, match="candidate_set_hash"):
        store.insert_evaluation(bad)
    forged = {**ev, "evaluation_id": "c" * 32, "verdict": {**ev["verdict"], "verdict": "PASS"}}
    with pytest.raises(StoreError, match="do not follow the policy"):
        store.insert_evaluation(forged)
    assert len(store.evaluations(old["run_id"])) == 1


def test_K02_the_real_value_quality_result_where_the_local_data_exists():
    db, bundle = ROOT / "runtime" / "preview_641.db", ROOT / "runtime" / "vq" / "vq_bundle.json"
    run_id = "1660bd5091ad4799a6908b9e8ddc6f2f"
    if not (db.exists() and bundle.exists()):
        pytest.skip("local data only: the Stage 6.4 run and its bundle are git-ignored runtime files")
    from strategy_validation.dataset import load
    from strategy_validation.store import StrategyValidationStore
    store = StrategyValidationStore(db)
    run = store.run(run_id)
    if run is None:
        pytest.skip("the Stage 6.4 run is not in this local database")
    before = _row_hash(db, run_id)
    ev = CS.assess_stored_run(run, store.candidates(run_id), store.trades(run_id, "RULES"), load(str(bundle)), ValueQualityAdapter(), registry=R.by_id(), created_at=AT)
    oo = ev["cross_sectional"]["segments"]["OOS"]
    v2 = run["diagnostics"]["strategy"]["portfolio"]["OOS"]                                        # the Stage 6.4 (validation_v2) diagnostics, as stored
    assert abs(v2["strategy"]["total_return"] - 0.532) < 0.001 and abs(v2["universe_equal_weight"]["total_return"] - 0.512) < 0.001
    assert abs(oo["strategy_total_return"] - v2["strategy"]["total_return"]) < 0.025 and abs(oo["control_total_return"] - v2["universe_equal_weight"]["total_return"]) < 0.025
    assert ev["selection_edge"]["status"] == "NOT_ESTABLISHED" and oo["excess_t_stat"] < 1.0 and ev["verdict"]["verdict"] == "FAIL"
    assert ev["performance_verdict"]["verdict"] == "PASS" and ev["cross_sectional"]["parity"]["status"] == "PASS" and R.get("VALUE_QUALITY").status == "RESEARCH"
    assert _row_hash(db, run_id) == before


def test_K03_an_evaluation_cannot_shadow_another_or_reassess_a_v3_run(tmp_path, monkeypatch):
    from strategy_validation.store import StoreError, StrategyValidationStore
    monkeypatch.setenv("STOCK_AGENT_ENV", "test")
    ds, run = long_run()
    old = _as_v2(run)
    db = tmp_path / "v.db"
    _store_raw(db, old)
    store = StrategyValidationStore(db)
    stored = store.run(old["run_id"])
    ev = CS.assess_stored_run(stored, store.candidates(old["run_id"]), store.trades(old["run_id"], "RULES"), ds, ValueQualityAdapter({"top_n": 5}), created_at=AT)
    with pytest.raises(StoreError, match="in the future"):
        store.insert_evaluation({**ev, "created_at": "9999-01-01T00:00:00+00:00"})
    store.insert_evaluation(ev)
    second = {**ev, "evaluation_id": "f" * 32, "created_at": "2026-01-01T00:00:00+00:00"}        # an older timestamp, inserted later
    store.insert_evaluation(second)
    assert store.latest_evaluation(old["run_id"])["evaluation_id"] == "f" * 32                     # insertion order, never a caller's timestamp
    v3 = rec_of(run)
    v3db = tmp_path / "v3.db"
    StrategyValidationStore(v3db).insert(run)
    with pytest.raises(CS.EvaluationError, match="carries its own selection-edge assessment"):
        CS.assess_stored_run(StrategyValidationStore(v3db).run(run.run_id), v3["candidates"], v3["arms"]["RULES"]["trades"], ds, ValueQualityAdapter({"top_n": 5}))
    with pytest.raises(StoreError, match="only for a run of an older methodology"):
        StrategyValidationStore(v3db).insert_evaluation({**ev, "run_id": run.run_id, "evaluated_run_validation_version": "validation_v3"})


def test_L01_older_records_stay_readable_and_are_never_reinterpreted(tmp_path, monkeypatch):
    from strategy_validation import service as SV
    from strategy_validation.store import StrategyValidationStore
    monkeypatch.setenv("STOCK_AGENT_ENV", "test")
    ds, run = long_run()
    old = _as_v2(run)
    db = tmp_path / "v.db"
    _store_raw(db, old)
    latest = StrategyValidationStore(db).latest(old["strategy_id"])
    assert latest["methodology_evaluation"] is None
    s = SV.summary(latest, old["strategy_id"])
    assert (s["validation_status"], s["selection_edge_status"], s["selection_edge_label"], s["methodology_version"]) == ("PASS", None, "Not assessed", "validation_v2")
    chk = IG.selection_control(StrategyValidationStore(db).run(old["run_id"]))
    assert chk.status == "NOT_APPLICABLE" and "never back-filled" in chk.detail
    assert SV.detail(latest, old["strategy_id"])["cross_sectional"] is None


# ================================================================ M-N event-driven + isolation ======================================================

def test_M01_event_driven_strategies_keep_the_per_trade_methodology():
    for name in ("GOOD", "OVERFIT", "TOO_SMALL"):
        ds, ad = FX.fixture(name)
        r = E.run_validation(ad, ds, created_at=AT)
        assert r.evaluation_mode == "EVENT_DRIVEN" and r.selection_edge["status"] == "NOT_APPLICABLE" and r.cross_sectional == {}
        assert r.verdict == {**r.performance_verdict, "basis": "PERFORMANCE"} and r.verdict["verdict"] == r.arms["RULES"].verdict["verdict"]
        assert {c.check: c.status for c in IG.evaluate(rec_of(r)).checks}["SELECTION_CONTROL"] == "NOT_APPLICABLE"
    ds, ad = FX.fixture("GOOD")
    assert E.run_validation(ad, ds, created_at=AT).verdict["verdict"] == "PASS"                   # the Stage 6.2 PASS fixture still passes


def test_M02_a_cross_sectional_strategy_version_cannot_be_re_stored_as_event_driven(tmp_path, monkeypatch):
    from strategy_validation.store import StoreError, StrategyValidationStore
    monkeypatch.setenv("STOCK_AGENT_ENV", "test")
    ds, run = long_run()
    store = StrategyValidationStore(tmp_path / "v.db")
    store.insert(run)
    switched = type("Switched", (ValueQualityAdapter,), {"evaluation_mode": "EVENT_DRIVEN"})({"top_n": 5})
    r2 = E.run_validation(switched, ds, registry=R.by_id(), created_at=AT)
    assert r2.selection_edge["status"] == "NOT_APPLICABLE" and r2.verdict["verdict"] == "PASS"
    with pytest.raises(StoreError, match="its evaluation mode cannot change"):
        store.insert(r2)
    rec = rec_of(run)
    rec["config_snapshot"]["adapter"]["evaluation_mode"] = "EVENT_DRIVEN"
    assert IG.evaluate(rec).status == "BLOCKED"                                                    # the record and its adapter must agree


def test_N01_no_broker_execution_or_network_import():
    for rel in ("strategy_validation/cross_sectional.py", "strategy_validation/cli.py", "strategy_validation/store.py", "strategy_validation/integrity.py"):
        tree = ast.parse(now_text(rel))
        mods = {n.module or "" for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)} | {a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
        assert not any(m.startswith(("execution", "safety", "paper", "portfolio", "proposals", "api", "requests", "urllib", "httpx", "socket", "alpaca", "anthropic"))
                       for m in mods), (rel, mods)
    assert not re.search(r"submit_order|place_order|/v2/orders|enable_auto", now_text("strategy_validation/cross_sectional.py"))


# ================================================================ O-Z gate, store, policy, CLI, service, documents ===================================

def test_O01_the_integrity_gate_blocks_a_claim_that_does_not_follow_the_policy():
    ds, run = long_run()
    rec = rec_of(run)
    assert IG.evaluate(rec).status == "ELIGIBLE" and {c.check: c.status for c in IG.evaluate(rec).checks}["SELECTION_CONTROL"] == "PASS"
    claim = copy.deepcopy(rec)
    claim["selection_edge"]["status"] = "ESTABLISHED"
    claim["verdict"] = claim["performance_verdict"]
    el = IG.evaluate(claim)
    assert el.status == "BLOCKED" and any(r["code"] == "SELECTION_CONTROL" for r in el.reasons)
    pass_anyway = copy.deepcopy(rec)
    pass_anyway["verdict"] = pass_anyway["performance_verdict"]                                 # absolute PASS presented as the overall verdict
    assert IG.evaluate(pass_anyway).status == "BLOCKED"
    mism = copy.deepcopy(rec)
    mism["cross_sectional"]["parity"] = {**mism["cross_sectional"]["parity"], "status": "FAIL", "failed": ["SAME_FRICTION"]}
    assert IG.evaluate(mism).status == "BLOCKED"                                                   # a FAIL status that its own checks do not support
    mism["cross_sectional"]["parity"]["checks"] = [{**c, "status": "FAIL"} if c["check"] == "SAME_FRICTION" else c for c in mism["cross_sectional"]["parity"]["checks"]]
    assert IG.evaluate(mism).status == "BLOCKED"                                                   # a failed parity audit with an edge claim
    mism["selection_edge"] = CS.selection_edge(mism["cross_sectional"]["segments"], mism["cross_sectional"]["parity"])
    mism["verdict"] = CS.overall_verdict(mism["performance_verdict"], "INVALID")
    el2 = IG.evaluate(mism)
    assert el2.status == "ELIGIBLE" and "absolute research result stands" in {c.check: c.detail for c in el2.checks}["SELECTION_CONTROL"]


def test_O02_nothing_recorded_is_trusted_statistics_period_table_and_strategy_side_are_re_derived():
    ds, run = long_run()
    rec = rec_of(run)
    forged = copy.deepcopy(rec)                                                                    # made-up statistics with a policy-consistent claim
    for k in ("OOS", "ALL"):
        forged["cross_sectional"]["segments"][k].update(excess_t_stat=3.5, mean_excess_period_return=0.02, mean_excess_period_return_gross=0.02)
    forged["selection_edge"] = CS.selection_edge(forged["cross_sectional"]["segments"], forged["cross_sectional"]["parity"])
    forged["verdict"] = CS.overall_verdict(forged["performance_verdict"], forged["selection_edge"]["status"])
    assert forged["selection_edge"]["status"] != "NOT_ESTABLISHED"
    el = IG.evaluate(forged)
    assert el.status == "BLOCKED" and "re-derived from the period table" in {c.check: c.detail for c in el.checks}["SELECTION_CONTROL"]
    table_forged = copy.deepcopy(rec)                                                              # a consistent but invented period table
    for r in table_forged["cross_sectional"]["periods"]:
        if r["status"] == "INCLUDED":
            r["strategy"]["net"] = r["control"]["net"] + 0.02
            r["excess"] = 0.02
    inc = [r for r in table_forged["cross_sectional"]["periods"] if r["status"] == "INCLUDED"]
    ppy = CS.periods_per_year(table_forged["cross_sectional"]["periods"])
    table_forged["cross_sectional"]["segments"] = {k: CS.segment_stats([r for r in inc if k == "ALL" or r["segment"] == k], ppy) for k in ("TRAIN", "OOS", "ALL")}
    table_forged["selection_edge"] = CS.selection_edge(table_forged["cross_sectional"]["segments"], table_forged["cross_sectional"]["parity"])
    table_forged["verdict"] = CS.overall_verdict(table_forged["performance_verdict"], table_forged["selection_edge"]["status"])
    el2 = IG.evaluate(table_forged)
    assert el2.status == "BLOCKED" and "does not match the run's stored trades" in {c.check: c.detail for c in el2.checks}["SELECTION_CONTROL"]


def test_P03_no_rounding_before_a_threshold():
    rows = rows_of([0.0105, 0.0095, 0.0112, 0.0089, 0.0101, 0.0099, 0.0107, 0.0093, 0.0104, 0.0096, 0.0102, 0.0098])
    seg = CS.segment_stats(rows, 12.0)
    ex = [r["excess"] for r in rows]
    assert seg["excess_t_stat"] == statistics.mean(ex) / (statistics.stdev(ex) / math.sqrt(len(ex)))   # full precision, not rounded
    near = {**seg, "excess_t_stat": 1.99996}
    all24 = CS.segment_stats(rows_of([0.01] * 6 + [0.011] * 6 + [0.009] * 12), 12.0)
    e = CS.selection_edge({"TRAIN": seg, "OOS": near, "ALL": all24}, PARITY_OK)
    assert e["status"] == "WEAK" and e["reasons"][0]["code"] == "EXCESS_WEAK"                      # 1.99996 is not >= 2.0
    assert CS.selection_edge({"TRAIN": seg, "OOS": {**seg, "excess_t_stat": 0.99999}, "ALL": all24}, PARITY_OK)["status"] == "NOT_ESTABLISHED"


def test_P01_the_overall_verdict_policy_table():
    perf = lambda v: {"verdict": v, "label": v, "reasons": [], "warnings": []}   # noqa: E731
    want = {("PASS", "ESTABLISHED"): "PASS", ("MIXED", "ESTABLISHED"): "MIXED", ("PASS", "WEAK"): "MIXED", ("PASS", "NOT_ESTABLISHED"): "FAIL",
            ("MIXED", "NOT_ESTABLISHED"): "FAIL", ("PASS", "INSUFFICIENT_DATA"): "INSUFFICIENT_DATA", ("PASS", "INVALID"): "MIXED", ("FAIL", "ESTABLISHED"): "FAIL",
            ("INSUFFICIENT_DATA", "WEAK"): "INSUFFICIENT_DATA", ("INVALID", "ESTABLISHED"): "INVALID", ("PASS", "NOT_APPLICABLE"): "PASS"}
    for (p, e), v in want.items():
        out = CS.overall_verdict(perf(p), e)
        assert out["verdict"] == v, (p, e, out["verdict"])
    assert set(EDGE_STATUSES) == {"NOT_APPLICABLE", "INSUFFICIENT_DATA", "NOT_ESTABLISHED", "WEAK", "ESTABLISHED", "INVALID"}


def test_P02_the_selection_edge_policy_rules():
    strong = [0.012, 0.009, 0.011, 0.01, 0.013, 0.008, 0.01, 0.012, 0.009, 0.011, 0.01, 0.0105]
    seg = CS.segment_stats(rows_of(strong), 12.0)
    all24 = CS.segment_stats(rows_of(strong * 2), 12.0)
    assert CS.selection_edge({"TRAIN": seg, "OOS": seg, "ALL": all24}, PARITY_OK)["status"] == "ESTABLISHED"
    assert CS.selection_edge({"TRAIN": seg, "OOS": seg, "ALL": seg}, PARITY_OK)["reasons"][0]["code"] == "TOO_FEW_PERIODS"      # 12 < 24 in total: WEAK
    bad_train = CS.segment_stats(rows_of([-0.012, -0.009, -0.011, -0.01, -0.013, -0.008]), 12.0)
    e = CS.selection_edge({"TRAIN": bad_train, "OOS": seg, "ALL": all24}, PARITY_OK)
    assert e["status"] == "WEAK" and "TRAIN_CONTRADICTS" in [r["code"] for r in e["reasons"]] and bad_train["excess_t_stat"] <= CS_TRAIN_CONTRADICTION_T
    weak = CS.segment_stats(rows_of([0.01, -0.006, 0.008, -0.004, 0.009, -0.002, 0.007, -0.005, 0.006, 0.0, 0.004, -0.001]), 12.0)
    assert CS_T_NEGLIGIBLE <= weak["excess_t_stat"] < CS_T_ESTABLISHED and CS.selection_edge({"TRAIN": seg, "OOS": weak, "ALL": all24}, PARITY_OK)["status"] == "WEAK"
    neg = CS.segment_stats(rows_of([-0.001] * 12), 12.0)
    assert CS.selection_edge({"TRAIN": seg, "OOS": neg, "ALL": all24}, PARITY_OK)["reasons"][0]["code"] == "NO_OOS_EXCESS"
    fr = rows_of([-0.0005] * 12)
    for r in fr:
        r["excess_gross"] = 0.001                                                                 # positive before friction, negative after
    assert CS.selection_edge({"TRAIN": seg, "OOS": CS.segment_stats(fr, 12.0), "ALL": all24}, PARITY_OK)["reasons"][0]["code"] == "FRICTION_ERASES_EXCESS"
    assert (CS_MIN_OOS_PERIODS, CS_MIN_TOTAL_PERIODS, CS_T_NEGLIGIBLE, CS_T_ESTABLISHED, CS_TRAIN_CONTRADICTION_T, CS_CONCENTRATION_SHARE) == (12, 24, 1.0, 2.0, -2.0, 0.5)


def test_Q01_assessing_a_stored_run_refuses_what_cannot_be_done_honestly():
    ds, run = long_run()
    old = _as_v2(run)
    with pytest.raises(CS.EvaluationError, match="not the run's dataset"):
        CS.assess_stored_run(old, old["candidates"], old["arms"]["RULES"]["trades"], F.dataset(F.run_set()), ValueQualityAdapter({"top_n": 5}))
    with pytest.raises(CS.EvaluationError, match="do not reproduce"):
        CS.assess_stored_run(old, old["candidates"], old["arms"]["RULES"]["trades"], ds, ValueQualityAdapter({"top_n": 6}))   # another configuration
    gds, gad = FX.fixture("GOOD")
    with pytest.raises(CS.EvaluationError, match="not a cross-sectional"):
        CS.assess_stored_run(rec_of(E.run_validation(gad, gds, created_at=AT)), [], [], gds, gad)


def test_R01_cli_evaluate_reports_and_stores_a_methodology_evaluation(tmp_path, monkeypatch):
    from strategy_validation import cli
    from strategy_validation.dataset import dump
    monkeypatch.setenv("STOCK_AGENT_ENV", "test")
    ds = F.dataset(F.run_set(), end=LONG_END)
    p, db = tmp_path / "vq.json", tmp_path / "v.db"
    dump(ds, str(p))
    out = io.StringIO()
    assert cli.main(["validate", "--strategy", "VALUE_QUALITY", "--dataset", str(p), "--db", str(db), "--persist"], out=out) == 0
    text = out.getvalue()
    assert "selection edge (vs Equal-weight eligible universe):" in text and "stored run" in text, text
    run_id = re.search(r"stored run ([0-9a-f]{32})", text).group(1)
    out1 = io.StringIO()
    assert cli.main(["evaluate", "--run", run_id, "--dataset", str(p), "--db", str(db)], out=out1) == 1 and "carries its own" in out1.getvalue()
    old = _as_v2(E.run_validation(ValueQualityAdapter(), ds, registry=R.by_id(), created_at=AT))   # the registered configuration, as the CLI uses
    _store_raw(db, old)
    out2 = io.StringIO()
    code = cli.main(["evaluate", "--run", old["run_id"], "--dataset", str(p), "--db", str(db), "--persist", "--output", str(tmp_path / "ev.json")], out=out2)
    t2 = out2.getvalue()
    assert code == 0 and re.search(rf"run {old['run_id']} \(validation_v2\) -> validation_v3: selection edge [A-Z_]+; overall", t2) and "stored evaluation" in t2, t2
    assert "control parity: 8 / 8 checks pass" in t2 and json.loads((tmp_path / "ev.json").read_text(encoding="utf-8"))["run_id"] == old["run_id"]
    assert cli.main(["evaluate", "--run", "0" * 32, "--dataset", str(p), "--db", str(db)], out=io.StringIO()) == 1


def test_S01_the_strategy_lab_payload_separates_absolute_control_and_edge(tmp_path, monkeypatch):
    from strategy_validation import service as SV
    from strategy_validation.store import StrategyValidationStore
    monkeypatch.setenv("STOCK_AGENT_ENV", "test")
    ds, run = long_run()
    store = StrategyValidationStore(tmp_path / "v.db")
    store.insert(run)
    latest = store.latest("VALUE_QUALITY")
    d = SV.detail(latest, "VALUE_QUALITY", R.get("VALUE_QUALITY"))
    assert (d["validation_status"], d["performance_verdict"], d["selection_edge_status"], d["evaluation_mode"]) == ("FAIL", "PASS", "NOT_ESTABLISHED", "CROSS_SECTIONAL_PORTFOLIO")
    assert d["overall_verdict"]["verdict"] == "FAIL" and d["absolute_verdict"]["verdict"] == "PASS" and d["cross_sectional"]["control"]["role"] == "SELECTION_CONTROL"
    assert all("legs_detail" not in r["strategy"] and "positions_detail" not in r["control"] for r in d["cross_sectional"]["periods"])   # compact payload
    assert R.get("VALUE_QUALITY").status == "RESEARCH"
    js = "const P=require(process.argv[1]); const d=JSON.parse(require('fs').readFileSync(process.argv[2],'utf8')); process.stdout.write(P.validationHtml(d));"
    (tmp_path / "d.json").write_text(json.dumps(d), encoding="utf-8")
    r = subprocess.run(["node", "-e", js, str(ROOT / "frontend" / "product_ui.js"), str(tmp_path / "d.json")], capture_output=True, text=True, encoding="utf-8", cwd=ROOT)
    h = r.stdout
    assert r.returncode == 0 and h.index('data-pui-sv-section="absolute"') < h.index('data-pui-sv-section="control"') < h.index('data-pui-sv-section="edge"'), r.stderr[-500:]
    assert 'data-pui-sv-role="SELECTION_CONTROL"' in h and 'data-pui-sv-role="MARKET_BENCHMARK"' in h and "NOT SELECTION-EDGE EVIDENCE" in h
    assert 'data-pui-sv-edge="NOT_ESTABLISHED"' in h and "Eligible for review" not in h and 'data-pui-sv-portfolio' not in h


def test_S02_node_tests_of_the_cross_sectional_display():
    r = subprocess.run(["node", "--test", str(ROOT / "tests" / "frontend" / "cross_sectional.test.cjs")], capture_output=True, text=True, cwd=ROOT)
    assert r.returncode == 0 and re.search(r"pass 6\b", r.stdout) and re.search(r"fail 0\b", r.stdout), r.stdout[-3000:] + r.stderr[-2000:]


def test_Z01_the_policy_was_committed_before_it_was_applied():
    r = subprocess.run(["git", "show", "17e2cb4:strategy_validation/__init__.py"], cwd=ROOT, capture_output=True, text=True)
    if r.returncode != 0:
        pytest.skip("the pre-registration commit is not reachable (shallow clone)")
    frozen: dict = {}
    exec(compile(r.stdout, "frozen", "exec"), frozen)                                              # noqa: S102 - the repository's own file at a fixed commit
    import strategy_validation as SVP
    for k in ("CS_MIN_OOS_PERIODS", "CS_MIN_TOTAL_PERIODS", "CS_MIN_PERIODS_T", "CS_T_NEGLIGIBLE", "CS_T_ESTABLISHED", "CS_TRAIN_CONTRADICTION_T",
              "CS_CONCENTRATION_SHARE", "EDGE_STATUSES", "EVALUATION_MODES", "CAPITAL_CONVENTION", "VERSION"):
        assert frozen[k] == getattr(SVP, k), k
    doc = subprocess.run(["git", "show", "17e2cb4:DESIGN_641_CROSS_SECTIONAL_VALIDATION.md"], cwd=ROOT, capture_output=True, text=True).stdout
    assert "## 6. Selection-edge policy (fixed before it was applied to any strategy)" in doc and "## 10. Value + Quality under validation_v3" in doc


def test_Z02_documents_and_frozen_controls():
    d = now_text("DESIGN_641_CROSS_SECTIONAL_VALIDATION.md")
    for s in ("## 2. Evaluation modes", "## 3. The control contract", "## 6. Selection-edge policy", "## 8. Control-parity audit", "NOT SELECTION-EDGE EVIDENCE",
              "MARKET BENCHMARK", "## 10. Value + Quality under validation_v3", "NOT_ESTABLISHED", "## 12. Adversarial review", "Backtest results are hypothetical."):
        assert s in d, s
    h = now_text("STRATEGY_VALIDATION_HARNESS.md")
    for s in ("## 14. Cross-sectional portfolio strategies (Stage 6.4.1, validation_v3)", "SELECTION CONTROL", "MARKET BENCHMARK", "strategy_validation_evaluations"):
        assert s in h, s
    readme = now_text("README.md")
    sec = re.findall(r"(?ms)^## Cross-sectional validation \(Stage 6\.4\.1, validation_v3\)\n.*?(?=^## )", readme)
    assert len(sec) == 1 and "python -m strategy_validation evaluate" in sec[0] and "NOT_ESTABLISHED" in sec[0]
    assert readme.index("## Value + Quality strategy (Stage 6.4") < readme.index("## Cross-sectional validation (Stage 6.4.1") < readme.index("## Continuous integration (Stage 6.0.3")
    s14 = h[h.index("## 14. Cross-sectional portfolio strategies"):]
    assert not re.search(r"(?i)\b(guaranteed|profitable|buy now|sell now|regime)\b", sec[0] + s14)
    import execution as X
    import forward_validation as FV
    assert FV.frozen_hash() == FROZEN_HASH and X.MODES == ("MONITOR_ONLY", "PAPER_AUTO")
    assert "test_cross_sectional_641.py" in now_text(".github/ci/pytest_groups.json")
