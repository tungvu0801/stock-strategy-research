"""Stage 6.7 — Strategy #5: Breakout v1 (breakout/, strategy_registry/breakout.py, DESIGN_67_BREAKOUT.md) and the opt-in FIXED_SLOTS_CASH capital
basis of validation_v3 (strategy_validation/cross_sectional.py).

  S  the breakout: the prior-20-session reference (T never in it), strength, no future data, splits, provider artifacts, missing bars
  W  the weekly calendar
  E  eligibility and ranking: price, liquidity, one listing per issuer, ties, at most N, fewer than N, none
  C  cash: a fixed 5 % per holding, a partial portfolio, an all-cash week, continuation holdings
  L  the adapter: weekly legs, next-open entry, next-rebalance exit, friction, candidate stability and identity
  V  validation_v3 + universe_integrity_v1: the control, one observation per week, the cash-driven-excess guard, turnover, sensitivity on TRAIN
     only, concentration, exposure, follow-through, failed breakouts, the Momentum overlap, data quality, the cap; the default basis unchanged
  U  the independent audit (it re-derives the selection and the cash allocation; tampered records are caught; it never imports the strategy)
  G  the integrity gate, the registry, the Strategy Lab, the CLI, isolation, frozen controls

Fixtures are SYNTHETIC (tests/breakout_fixtures.py). The real run is reproduced by `python -m strategy_validation validate --strategy BREAKOUT
--dataset runtime/vq/vq_bundle.json`."""
from __future__ import annotations

import ast
import copy
import functools
import io
import json
import re
import socket
import statistics
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

import breakout as BO  # noqa: E402
import breakout_fixtures as F  # noqa: E402
from breakout import audit as BOA  # noqa: E402
from breakout.adapter import BreakoutAdapter, eligible_set_sha  # noqa: E402
from breakout.core import INELIGIBLE, BreakoutStrategyCore, iso_week, pct_ranks  # noqa: E402
from breakout.data import PriceData  # noqa: E402
from strategy_registry import registry as R  # noqa: E402
from strategy_validation import cross_sectional as CS  # noqa: E402
from strategy_validation import engine as E  # noqa: E402
from strategy_validation import integrity as IG  # noqa: E402
from strategy_validation.dataset import Dataset  # noqa: E402

AT = "2026-10-26T00:00:00+00:00"
FROZEN_HASH = "b5d4a56ef5bba61f8250b3cd25182cc1655738adf0605a77671fd029883f8530"
FREEZE = "2eb5981"                                                                              # "freeze Breakout v1 rules before OOS"
FREEZE_RUN_SET = "2a1f66f5c2024258d83fa6db0678cb45"                                             # the candidate set of the first OOS run (at the freeze)
D = F.weekdays("2019-01-02", "2022-12-30")
SCHED = BreakoutStrategyCore().schedule(D)


def now_text(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8").replace("\r\n", "\n")


@functools.lru_cache(maxsize=None)
def base_ds():
    return F.dataset(F.universe(25))


@functools.lru_cache(maxsize=None)
def full_run():
    ds = base_ds()
    return ds, E.run_validation(BreakoutAdapter({"max_holdings": 5}), ds, registry=R.by_id(), created_at=AT)


def sparse_specs(n=7, T=None):
    specs = F.falling(25)
    for k in range(n):
        specs[k]["bumps"] = [(T or SCHED[60], 1.25)]                                                # n stocks break out together at T
    return specs


@functools.lru_cache(maxsize=None)
def sparse_run():
    ds = F.dataset(sparse_specs())
    return ds, E.run_validation(BreakoutAdapter(), ds, registry=R.by_id(), created_at=AT)


@functools.lru_cache(maxsize=None)
def cash_run():
    ds = F.dataset(F.falling(25))
    return ds, E.run_validation(BreakoutAdapter(), ds, registry=R.by_id(), created_at=AT)


def rec_of(run) -> dict:
    return json.loads(run.model_dump_json())


def evaluate(ds, T, max_holdings=5, **kw):
    data = PriceData(ds)
    syms = [s for s in BO.RESEARCH_UNIVERSE if s in ds.bars]
    return BreakoutStrategyCore(max_holdings=max_holdings, **kw).evaluate(T, syms, data)


def rebuild(ds, fn_bar=None, fn_raw=None):
    bars = {s: [{"date": b.date, **{k: (fn_bar(s, b.date, getattr(b, k)) if fn_bar else getattr(b, k)) for k in ("open", "high", "low", "close")}, "volume": b.volume}
                for b in rows] for s, rows in ds.bars.items()}
    aux = copy.deepcopy(ds.aux)
    if fn_raw:
        for s, m in aux["series"]["raw_close"].items():
            for d in m:
                m[d] = fn_raw(s, d, m[d])
    return Dataset(bars, kind=ds.kind, source=ds.source, provenance=ds.declared, aux=aux)


def busy_T(ds, k0=30):
    """A rebalance with at least one breakout (the strongest stocks break out often)."""
    return next(t for t in BreakoutStrategyCore().schedule(ds.sessions)[k0:] if evaluate(ds, t)["ranked"])


# ================================================================ S the breakout ====================================================================

def test_S01_the_reference_is_the_highest_adjusted_high_of_the_20_sessions_before_T():
    ds = base_ds()
    T = busy_T(ds)
    i = ds.sessions.index(T)
    x = evaluate(ds, T)["ranked"][0]
    inp = x["snapshot"]["inputs"]
    highs = {b.date: b.high for b in ds.bars[x["symbol"]]}
    closes = {b.date: b.close for b in ds.bars[x["symbol"]]}
    assert (inp["reference_start_date"], inp["reference_end_date"]) == (ds.sessions[i - 20], ds.sessions[i - 1]) and inp["reference_end_date"] < T
    assert inp["breakout_reference"] == max(highs[d] for d in ds.sessions[i - 20:i]) and closes[T] > inp["breakout_reference"]
    assert x["strength"] == pytest.approx(closes[T] / inp["breakout_reference"] - 1.0) and BO.LOOKBACK_SESSIONS == 20
    assert evaluate(ds, T)["ranked"] == evaluate(ds, T)["ranked"]                                    # deterministic


def test_S02_the_session_itself_is_never_in_its_own_reference():
    T = SCHED[60]
    i = D.index(T)
    specs = F.falling(25)
    k = next(j for j, s in enumerate(specs) if s["symbol"] == "AAPL")
    specs[k] = F.stock("AAPL", drift=0.0, wave=0.0, price=90.0, override={D[i - 5]: {"high": 100.0}, T: {"open": 90.0, "high": 120.0, "low": 90.0, "close": 110.0}})
    ds = F.dataset(specs)
    out = evaluate(ds, T)
    x = next(x for x in out["ranked"] if x["symbol"] == "AAPL")
    assert x["snapshot"]["inputs"]["breakout_reference"] == 100.0 and x["strength"] == pytest.approx(0.10) and "AAPL" in out["selected"]   # 110 > 100
    highs = {b.date: b.high for b in ds.bars["AAPL"]}
    assert max(highs[d] for d in D[i - 20:i + 1]) == 120.0 > 110.0                                 # including T would have hidden the breakout


def test_S03_rewriting_every_price_after_T_leaves_the_candidates_ranks_and_portfolio_byte_identical():
    ds = base_ds()
    T = busy_T(ds)
    future = rebuild(ds, fn_bar=lambda s, d, v: v * (0.1 if d > T else 1.0) * (9.0 if (d > T and s in ("AAPL", "ABBV")) else 1.0),
                     fn_raw=lambda s, d, v: v * (0.1 if d > T else 1.0))
    a, b = evaluate(ds, T), evaluate(future, T)
    key = lambda o: json.dumps([(x["symbol"], x["strength"], x["percentile"], x["rank"], x["snapshot"]["inputs"]) for x in o["ranked"]] + [o["selected"], o["eligible"]],  # noqa: E731
                               sort_keys=True)
    assert key(a) == key(b)
    ca = [c for c in BreakoutAdapter({"max_holdings": 5}).candidates(ds) if c.signal_date == T]
    cb = [c for c in BreakoutAdapter({"max_holdings": 5}).candidates(future) if c.signal_date == T]
    assert json.dumps([(c.symbol, c.score, c.features) for c in ca], sort_keys=True) == json.dumps([(c.symbol, c.score, c.features) for c in cb], sort_keys=True)


def test_S04_a_split_never_creates_or_removes_a_breakout():
    plain = F.universe(25)
    split = copy.deepcopy(plain)
    split[3]["split"] = (D[300], 4.0)                                                               # ACN splits 4-for-1: the raw price falls 75 %
    a, b = F.dataset(plain), F.dataset(split)
    for T in [t for t in SCHED if D[290] <= t <= D[330]]:                                         # every window around the split
        ea, eb = evaluate(a, T), evaluate(b, T)
        assert [(x["symbol"], round(x["strength"], 6)) for x in ea["ranked"]] == [(x["symbol"], round(x["strength"], 6)) for x in eb["ranked"]], T
        assert ea["selected"] == eb["selected"] and ea["eligible"] == eb["eligible"]
    assert b.aux["series"]["raw_close"]["ACN"][D[300]] < b.aux["series"]["raw_close"]["ACN"][D[299]] / 3


def test_S05_a_provider_artifact_never_becomes_a_breakout_and_is_never_clipped():
    T = SCHED[60]
    base = F.falling(25)
    k = next(j for j, s in enumerate(base) if s["symbol"] == "HON")
    for steps, code in (({"adj_steps": [(T, 0.85)]}, "LARGE_REBASING"),                             # the adjusted history re-based down: a +17.6 % step
                        ({"adj_steps": [(T, 0.85)], "ratio_steps": [(T, 0.5)]}, "RATIO_ARTIFACT")):  # ... with the HON-type ratio move
        specs = copy.deepcopy(base)
        specs[k].update(steps)
        ds = F.dataset(specs)
        data = PriceData(ds)
        i = ds.sessions.index(T)
        assert data.adj_close("HON", T) > max(data.adj_high("HON", d) for d in ds.sessions[i - 20:i])   # without the rule: a fake breakout
        out = evaluate(ds, T)
        assert "HON" not in out["eligible"] and "HON" not in out["selected"] and {x["symbol"]: x["code"] for x in out["data_exclusions"]}["HON"] == code
        snap = BreakoutStrategyCore().snapshot("HON", T, data)
        assert snap["reason"] == "DATA_INCONSISTENT" and snap["strength"] is None and snap["inputs"] == {}   # flagged, never a clipped breakout
    assert evaluate(F.dataset(base), T)["ranked"] == [] and "DATA_INCONSISTENT" in INELIGIBLE


def test_S06_a_missing_bar_or_short_history_is_excluded_never_filled():
    T = busy_T(base_ds())
    i = D.index(T)
    for gap, reason in ((D[i - 7], "MISSING_LOOKBACK_BAR"), (T, "NO_PRICE")):
        g = F.universe(25)
        g[6]["gaps"] = [gap]                                                                         # AMAT: a hole in the reference window / on T
        o = evaluate(F.dataset(g), T)
        assert "AMAT" not in o["eligible"] and o["summary"]["ineligible_by_reason"][reason] == 1, reason
    late = F.universe(25)
    late[5]["start"] = D[i - 10]                                                                     # ADI listed 10 sessions before T
    o = evaluate(F.dataset(late), T)
    assert "ADI" not in o["eligible"] and o["summary"]["ineligible_by_reason"] == {"LIQUIDITY_HISTORY": 1}


# ================================================================ W the weekly calendar ==============================================================

def test_W01_one_decision_per_iso_week_monday_holiday_short_week_and_year_boundary():
    hol = ("2024-01-01", "2024-01-15", "2024-11-28", "2024-11-29", "2024-12-25", "2025-01-01")
    sessions = F.weekdays("2023-11-01", "2025-01-31", hol)
    sched = BreakoutStrategyCore().schedule(sessions)
    weeks = [iso_week(s) for s in sched]
    first = {}
    for i, s in enumerate(sessions):
        first.setdefault(iso_week(s), i)
    assert len(weeks) == len(set(weeks)) and set(weeks) == {w for w, i in first.items() if i >= 20}
    assert "2024-01-16" in sched and "2024-01-02" in sched and "2024-11-25" in sched and "2024-12-30" in sched and "2025-01-02" not in sched
    assert sched == BOA.schedule(sessions, BO.LOOKBACK_SESSIONS)                                       # the audit's independent calendar agrees


# ================================================================ E eligibility + ranking ===========================================================

def test_E01_price_and_liquidity_filters_and_one_listing_per_issuer():
    T = busy_T(base_ds())
    cos = F.universe(25)
    cos[0]["price"] = 2.0
    cos[1]["volume"] = 1000.0
    cos[2]["cik"] = cos[3]["cik"]
    out = evaluate(F.dataset(cos), T)
    r = out["summary"]["ineligible_by_reason"]
    assert r == {"PRICE_BELOW_MIN": 1, "LIQUIDITY_BELOW_MIN": 1, "SECOND_LISTING": 1} and set(r) <= set(INELIGIBLE)
    assert {"AAPL", "ABBV", "ACN"}.isdisjoint(out["eligible"]) and "ABT" in out["eligible"]


def test_E02_only_breakouts_are_ranked_the_strongest_first_ties_by_symbol():
    snaps = [{"symbol": s, "eligible": True, "breakout": b, "strength": st, "inputs": {}, "anomaly": None, "reason": None}
             for s, b, st in (("D", True, 0.05), ("B", True, 0.10), ("C", False, None), ("A", True, 0.10), ("E", True, 0.01))]
    ranked = BreakoutStrategyCore().rank(snaps)
    assert [x["symbol"] for x in ranked] == ["A", "B", "D", "E"] and "C" not in [x["symbol"] for x in ranked]   # never a non-breakout
    assert [x["rank"] for x in ranked] == [1, 2, 3, 4] and ranked[0]["percentile"] == ranked[1]["percentile"] == pytest.approx(3.5 / 4)
    assert pct_ranks([("B", 0.1), ("A", 0.1), ("C", 0.3)]) == {"A": 0.5, "B": 0.5, "C": 1.0}


@pytest.mark.parametrize("n,want", [(25, 20), (20, 20), (7, 7), (0, 0)])
def test_E03_at_most_20_all_of_fewer_none_when_nothing_breaks_out(n, want, monkeypatch):
    core = BreakoutStrategyCore()
    syms = list(F.SYMBOLS[:25])
    fake = {s: {"symbol": s, "eligible": True, "breakout": j < n, "strength": (0.3 - j * 0.01) if j < n else None, "inputs": {"breakout_reference": 1.0},
                "anomaly": None, "reason": None} for j, s in enumerate(syms)}
    monkeypatch.setattr(core, "snapshot", lambda s, T, data, lr=None: fake[s])
    out = core.evaluate(D[100], syms, PriceData(base_ds()))
    assert len(out["selected"]) == want and out["summary"]["breakout_candidates"] == n and out["summary"]["invested_fraction"] == want / 20
    assert out["selected"] == syms[:want] and len(out["eligible"]) == 25                             # the strongest; never filled with non-breakouts
    assert (BO.MAX_HOLDINGS, BO.SLOT_PCT, BO.CASH_RETURN) == (20, 5.0, 0.0)


# ================================================================ C cash =============================================================================

def test_C01_seven_breakouts_hold_seven_at_5_percent_each_and_65_percent_cash():
    ds, run = sparse_run()
    T = SCHED[60]
    p = next(x for x in run.cross_sectional["periods"] if x["rebalance"] == T)
    legs = [t for t in run.arms["RULES"].trades if t.signal_date == T]
    assert p["status"] == "INCLUDED" and p["strategy"]["positions"] == 7 and p["strategy"]["invested_fraction"] == pytest.approx(0.35)
    assert p["strategy"]["net"] == pytest.approx(sum(t.trade_return for t in legs) / 20, abs=1e-12)    # cash at 0 %, never renormalized
    assert p["strategy"]["net"] != pytest.approx(sum(t.trade_return for t in legs) / 7, abs=1e-6)
    assert all(4800 < t.entry_fill * t.qty <= 5000 for t in legs)                                    # 5 % of the 100,000 equity, not 14.3 %
    assert run.config_snapshot["sizing"]["position_pct"] == 5.0 and run.cross_sectional["control"]["strategy_capital"] == \
        {"basis": "FIXED_SLOTS_CASH", "slots": 20, "slot_pct": 5.0, "cash_return": 0.0}


def test_C02_a_week_with_no_breakout_is_an_all_cash_observation_never_a_failure():
    ds, run = cash_run()
    cs = run.cross_sectional
    assert run.candidate_count == 0 and cs["parity"]["status"] == "PASS" and IG.evaluate(rec_of(run)).status == "ELIGIBLE"
    inc = [p for p in cs["periods"] if p["status"] == "INCLUDED"]
    assert inc and all(p["strategy"]["positions"] == 0 and p["strategy"]["net"] == 0.0 and p["strategy"]["invested_fraction"] == 0.0 for p in inc)
    assert all(p["excess"] == pytest.approx(-p["control"]["net"]) for p in inc) and cs["excluded_periods"] == {"BOUNDARY": 1}
    ds2, run2 = sparse_run()
    zero = [p for p in run2.cross_sectional["periods"] if p["status"] == "INCLUDED" and p["strategy"]["positions"] == 0]
    assert zero and all(p["strategy"]["net"] == 0.0 for p in zero)                                  # a partly-invested run: the other weeks are cash


def test_C03_the_cash_driven_excess_guard_never_reads_cash_as_selection():
    ds, run = cash_run()
    oo = run.cross_sectional["segments"]["OOS"]
    assert oo["mean_excess_period_return"] > 0 and oo["invested_capital_weeks"] == 0                 # cash beat a falling control ...
    edge = run.cross_sectional["selection_edge"]
    assert edge["status"] == "NOT_ESTABLISHED" and edge["reasons"][0]["code"] == "CASH_DRIVEN_EXCESS"   # ... which is not selection
    segs = {"OOS": {**oo, "excess_t_stat": 9.0}, "TRAIN": run.cross_sectional["segments"]["TRAIN"], "ALL": run.cross_sectional["segments"]["ALL"]}
    assert CS.selection_edge(segs, {"status": "PASS"})["status"] == "NOT_ESTABLISHED"              # however strong the t
    ok = {**segs["OOS"], "selection_part_mean": 0.004, "selection_part_t": 2.5}
    assert CS.selection_edge({**segs, "OOS": ok}, {"status": "PASS"})["status"] != "NOT_ESTABLISHED"
    weak = {**segs["OOS"], "selection_part_mean": 0.004, "selection_part_t": 0.6}                    # a positive but negligible selection part
    assert CS.selection_edge({**segs, "OOS": weak}, {"status": "PASS"})["reasons"][0]["code"] == "CASH_DRIVEN_EXCESS"


def test_C05_cash_timing_is_not_selection_the_review_counter_example():
    """Up weeks fully invested with a losing selection, down weeks 1 position with a winning one: the UNWEIGHTED mean over weeks with a position is
    positive, the capital-weighted selection part is negative, the period excess (all from cash in down weeks) is large - never an edge."""
    rows = []
    for k in range(120):
        up = k % 2 == 0
        c = 0.02 if up else -0.02
        n, legs = (20, c - 0.002) if up else (1, c + 0.004)
        net = n * legs / 20
        rows.append({"rebalance": f"2025-{1 + k // 28:02d}-{1 + k % 28:02d}", "segment": "OOS", "excess": net - c, "excess_gross": net - c,
                     "strategy": {"net": net, "gross": net, "positions": n, "entries": 0, "slots": 20, "invested_fraction": n / 20},
                     "control": {"net": c, "gross": c, "positions": 70, "entries": 0}, "benchmark": {"return": c}})
    s = CS.segment_stats(rows, 52.0)
    assert s["invested_capital_mean_excess"] > 0 and s["selection_part_mean"] < 0 and s["mean_excess_period_return"] > 0 and s["excess_t_stat"] > 2
    assert s["selection_part_mean"] + s["cash_part_mean"] == pytest.approx(s["mean_excess_period_return"], abs=1e-12)   # the exact split
    edge = CS.selection_edge({"OOS": s, "TRAIN": s, "ALL": s}, {"status": "PASS"})
    assert edge["status"] == "NOT_ESTABLISHED" and edge["reasons"][0]["code"] == "CASH_DRIVEN_EXCESS"


def test_C04_a_continuing_holding_pays_no_friction_and_flags_follow_the_selection():
    ds, run = full_run()
    slip = run.friction_model["slippage_bps_per_side"] / 10000.0
    legs = {c.candidate_id: c for c in run.candidates}
    holds = 0
    for t in run.arms["RULES"].trades:
        c = legs[t.candidate_id]
        assert t.entry_fill == pytest.approx(t.entry_raw * (1 + slip) if c.entry_trade else t.entry_raw)
        assert t.exit_fill == pytest.approx(t.exit_raw * (1 - slip) if c.exit_trade else t.exit_raw)
        holds += not c.entry_trade
    assert holds > 0 and run.execution_assumptions["stop_policy"] == "NONE"


# ================================================================ L the adapter =====================================================================

def test_L01_weekly_legs_enter_at_the_next_open_and_exit_at_the_open_after_the_next_rebalance():
    ds, run = full_run()
    sched = BreakoutStrategyCore().schedule(ds.sessions)
    trades = {t.candidate_id: t for t in run.arms["RULES"].trades}
    seen = 0
    for c in run.candidates[:200]:
        t = trades.get(c.candidate_id)
        if t is None:
            continue
        seen += 1
        k = sched.index(c.signal_date)
        assert t.entry_date == ds.sessions[ds.sessions.index(c.signal_date) + 1] > c.signal_date and t.entry_raw == ds.bar(c.symbol, t.entry_date).open
        if k + 1 < len(sched):
            assert t.exit_date == ds.sessions[ds.sessions.index(sched[k + 1]) + 1] and t.exit_raw == ds.bar(c.symbol, t.exit_date).open
    assert seen > 100


def test_L02_candidates_are_stable_identical_across_runs_and_every_arm_gets_the_same_set():
    ds, run = full_run()
    again = E.run_validation(BreakoutAdapter({"max_holdings": 5}), F.dataset(F.universe(25)), registry=R.by_id(), created_at=AT)
    assert run.candidate_set_hash == again.candidate_set_hash and [c.candidate_id for c in run.candidates] == [c.candidate_id for c in again.candidates]
    assert run.cross_sectional["segments"] == again.cross_sectional["segments"] and run.integrity["same_candidate_set"]
    assert run.arms["GATED"].metrics == run.arms["RULES"].metrics and run.arms["MODEL"].status == "NOT_EVALUATED"
    assert len({(c.symbol, c.signal_date) for c in run.candidates}) == run.candidate_count
    f = run.candidates[0].features
    assert {"eligible_count", "breakout_candidate_count", "selected_count", "breakout_reference", "breakout_strength", "eligible_set_sha"} <= set(f)


# ================================================================ V validation_v3 + diagnostics =====================================================

def test_V01_the_control_is_every_eligible_stock_and_every_parity_check_passes():
    ds, run = full_run()
    cs = run.cross_sectional
    assert cs["control"]["control_id"] == "EQUAL_WEIGHT_ELIGIBLE_UNIVERSE" and cs["control"]["role"] == "SELECTION_CONTROL" and cs["market_benchmark"]["role"] == "MARKET_BENCHMARK"
    assert cs["parity"]["status"] == "PASS" and len(cs["parity"]["checks"]) == 8 and run.diagnostics["strategy"]["control_membership"]["mismatches"] == 0
    snaps = {s["rebalance"]: s for s in BreakoutAdapter({"max_holdings": 5}).selection_snapshots(ds)}
    for c in run.candidates:
        el = snaps[c.signal_date]["eligible"]
        assert c.features["eligible_set_sha"] == eligible_set_sha(el) and c.features["eligible_count"] == len(el) >= c.features["breakout_candidate_count"]
    assert any(len(s["eligible"]) > len(s["selected"]) for s in snaps.values())                     # the control holds non-breakouts too


def test_V02_one_observation_per_week_on_the_fixed_slot_basis():
    ds, run = full_run()
    cs = run.cross_sectional
    inc = [p for p in cs["periods"] if p["status"] == "INCLUDED"]
    assert cs["segments"]["ALL"]["period_count"] == len(inc) and 50 < cs["periods_per_year"] < 54 and cs["excluded_periods"] == {"BOUNDARY": 1}
    trades = {}
    for t in run.arms["RULES"].trades:
        trades.setdefault(t.signal_date, []).append(t.trade_return)
    assert all(p["strategy"]["net"] == pytest.approx(sum(trades.get(p["rebalance"], [])) / 5, abs=1e-12) for p in inc)
    assert run.diagnostics["strategy"]["cash_accounting"]["mismatches"] == 0


def test_V03_the_default_basis_is_unchanged_and_the_gate_checks_the_declared_basis():
    assert CS.slots_of(None) == 0 and CS.slots_of({"basis": "OTHER", "slots": 20}) == 0 and CS.slots_of({"basis": "FIXED_SLOTS_CASH", "slots": 20}) == 20
    import mean_reversion_fixtures as MRF
    from mean_reversion.adapter import MeanReversionAdapter
    mr = E.run_validation(MeanReversionAdapter({"top_n": 5}), MRF.dataset(MRF.universe(25)), registry=R.by_id(), created_at=AT)
    assert "strategy_capital" not in mr.cross_sectional["control"] and all("invested_fraction" not in p["strategy"] for p in mr.cross_sectional["periods"])
    assert "invested_capital_weeks" not in mr.cross_sectional["segments"]["OOS"] and IG.evaluate(rec_of(mr)).status == "ELIGIBLE"
    ds, run = full_run()
    rec = rec_of(run)
    forged = copy.deepcopy(rec)
    forged["cross_sectional"]["control"]["strategy_capital"]["slots"] = 1                             # a forged basis: legs read as the whole capital
    el = IG.evaluate(forged)
    assert el.status == "BLOCKED" and "not the adapter's own" in {c.check: c.detail for c in el.checks}["SELECTION_CONTROL"]
    renorm = rec_of(sparse_run()[1])
    p = next(x for x in renorm["cross_sectional"]["periods"] if x["status"] == "INCLUDED" and x["strategy"]["positions"] == 7)
    p["strategy"]["net"] = p["strategy"]["net"] * 20 / 7                                             # 7 holdings renormalized to 100 % invested
    p["excess"] = p["strategy"]["net"] - p["control"]["net"]
    assert any("strategy side" in x for x in CS.verify(renorm["cross_sectional"], renorm["selection_edge"], renorm["performance_verdict"], renorm["verdict"],
                                                       trades=renorm["arms"]["RULES"]["trades"]))


def test_V04_turnover_friction_exposure_and_absolute_diagnostics():
    ds, run = sparse_run()
    d = run.diagnostics["strategy"]
    ex = d["exposure"]["ALL"]
    assert set(ex["weeks_by_positions"]) == {"0", "1-5", "6-10", "11-19", "20"} and ex["weeks_by_positions"]["6-10"] >= 1 and ex["weeks_by_positions"]["0"] >= 1
    assert sum(ex["weeks_by_positions"].values()) == ex["weeks"] and ex["max_invested_fraction"] == pytest.approx(0.35) and ex["min_invested_fraction"] == 0.0
    c = d["construction"]
    assert c["average_one_way_turnover"] is not None and "entries / N slots" in c["turnover_basis"]
    assert set(d["portfolio"]["ALL"]) == {"strategy", "selection_control", "market_benchmark"} and "strategy_beta_to_spy" in d["market_sensitivity"]["ALL"]
    assert set(d["market_weeks"]["ALL"]) == {"benchmark_up", "benchmark_down"} and d["invested_capital"]["ALL"]["weeks_with_a_position"] >= 1
    ic = d["invested_capital"]["ALL"]
    assert ic["selection_part_mean"] + ic["cash_part_mean"] == pytest.approx(statistics.mean(p["excess"] for p in run.cross_sectional["periods"] if p["status"] == "INCLUDED"), abs=1e-5)
    oo = run.cross_sectional["segments"]["ALL"]
    assert oo["strategy_turnover"] == pytest.approx(statistics.mean(p["strategy"]["entries"] / 20 for p in run.cross_sectional["periods"] if p["status"] == "INCLUDED"), abs=1e-4)


def test_V05_the_sensitivity_never_sees_a_bar_after_the_train_end():
    ds, run = full_run()
    s = run.diagnostics["strategy"]["sensitivity"]
    assert [r["params"] for r in s["rows"]] == ["base"] + list(BO.SENSITIVITY) and "TRAIN view only" in s["scope"] and len({r["periods"] for r in s["rows"]}) == 1
    assert BO.SENSITIVITY == ({"lookback_sessions": 10}, {"lookback_sessions": 40}, {"max_holdings": 15}, {"max_holdings": 25})
    wrecked = rebuild(ds, fn_bar=lambda sym, d, v: v * (3.0 if d > run.train_end else 1.0), fn_raw=lambda sym, d, v: v * (3.0 if d > run.train_end else 1.0))
    from breakout import diagnostics as BOD
    base = {"max_holdings": 5, "lookback_sessions": 20}
    assert BOD.sensitivity(wrecked, run.train_end, 0.0005, base) == BOD.sensitivity(ds, run.train_end, 0.0005, base)


def test_V06_concentration_names_plus_cash_equal_the_summed_excess():
    ds, run = full_run()
    c = run.diagnostics["strategy"]["concentration"]
    ec = c["excess_contribution"]
    assert c["sic_major_group"]["most_frequent_top_groups"] and c["most_held_names"] and ec["top"]
    assert ec["names"] + ec["cash"] == pytest.approx(ec["summed_period_excess"], abs=1e-5)
    assert (BO.FLAG_NAMES in run.research_flags) == (ec["top_five_share_of_summed_excess"] is not None and ec["top_five_share_of_summed_excess"] >= 0.5)


def test_V07_follow_through_failed_breakouts_and_the_momentum_overlap_are_diagnostics_only():
    ds, run = full_run()
    d = run.diagnostics["strategy"]
    f = d["follow_through"]
    assert f["continued_higher"] + f["flat"] + f["failed"] == pytest.approx(1.0, abs=1e-3) and f["band"] == BO.FOLLOW_THROUGH_BAND
    fb = d["failed_breakouts"]
    assert 0 <= fb["share_ending_below_entry_price"] <= 1 and 0 <= fb["share_ending_below_breakout_reference"] <= 1
    ov = d["momentum_overlap"]
    assert ov["weeks"] > 0 and 0 <= ov["share_of_breakout_holdings_in_momentum_portfolio"] <= 1 and "never combined" in ov["note"]
    assert 0 < ov["share_expected_by_chance"] <= 1 and set(f["relative_to_control"]) == {"above_control", "within_band", "below_control", "mean"}
    src = now_text("breakout/core.py") + now_text("breakout/adapter.py") + now_text("breakout/data.py")
    assert not re.search(r"(?m)^\s*(from|import) (momentum|mean_reversion|value_quality)", src)       # the rules never use another strategy


def test_V08_the_universe_is_fixed_current_and_a_raw_established_edge_is_capped():
    ds, run = full_run()
    d = BreakoutAdapter.universe_declaration
    assert d["universe_method"] == "FIXED_CURRENT_UNIVERSE" and d["survivorship_bias"] == "PRESENT" and d["coverage_start"] is None
    assert run.cross_sectional["selection_edge"]["status"] == "ESTABLISHED" and run.cross_sectional["segments"]["OOS"]["invested_capital_mean_excess"] > 0
    assert run.selection_edge["status"] == "NOT_ESTABLISHED" and run.selection_edge["capped"] and run.selection_edge["reasons"][0]["code"] == "UNIVERSE_INTEGRITY_CAP"
    assert run.verdict["verdict"] == "FAIL" and run.universe_integrity["status"] == "UNTRUSTED" and "SURVIVORSHIP_BIAS_PRESENT" in run.research_flags
    import value_quality as VQ
    assert BO.RESEARCH_UNIVERSE == VQ.RESEARCH_UNIVERSE and R.get("BREAKOUT").status == "RESEARCH"


def test_V09_the_data_quality_diagnostics_report_exclusions_and_held_window_artifacts():
    specs = F.universe(25)
    k = next(j for j, s in enumerate(specs) if s["symbol"] == "HON")
    specs[k]["ratio_steps"], specs[k]["adj_steps"] = [(D[300], 0.5)], [(D[300], 1.048)]             # a HON-type step down inside held windows
    run = E.run_validation(BreakoutAdapter({"max_holdings": 5}), F.dataset(specs), registry=R.by_id(), created_at=AT)
    dq = run.diagnostics["strategy"]["data_quality"]
    assert dq["data_inconsistent_exclusions"] >= 1 and set(dq["by_code"]) == {"RATIO_ARTIFACT"} and [a["symbol"] for a in dq["ratio_artifacts"]] == ["HON"]
    assert any(x["side"] == "control" and x["symbol"] == "HON" for x in dq["holding_window_anomalies"]) and BO.FLAG_HOLDING_ANOMALY in run.research_flags
    assert all(x["code"] != "LARGE_REBASING" for x in dq["holding_window_anomalies"])               # an adjusted distribution is total return, not an error
    sp = F.universe(25)
    sp[k]["adj_steps"] = [(D[300], 0.85)]                                                            # an ADJUSTED large distribution only (a spin-off)
    run2 = E.run_validation(BreakoutAdapter({"max_holdings": 5}), F.dataset(sp), registry=R.by_id(), created_at=AT)
    dq2 = run2.diagnostics["strategy"]["data_quality"]
    assert dq2["holding_window_anomaly_count"] == 0 and BO.FLAG_HOLDING_ANOMALY not in run2.research_flags
    assert dq2["adjusted_distributions_in_held_windows"] and all("excess_at_stake_if_misadjusted" in x for x in dq2["adjusted_distributions_in_held_windows"])
    assert run.audit.status == "PASS"


# ================================================================ U the independent audit ===========================================================

def test_U01_the_audit_re_derives_the_selection_and_never_imports_the_strategy():
    ds, run = full_run()
    assert BOA.audit(rec_of(run), ds, max_holdings=5, lookback=20) == [] and run.audit.status == "PASS"
    for r in (sparse_run(), cash_run()):
        assert r[1].audit.status == "PASS"
    tree = ast.parse(now_text("breakout/audit.py"))
    mods = {n.module or "" for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)} | {a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
    assert not any(m.startswith(("breakout.core", "breakout.data", "breakout.adapter", "momentum", "mean_reversion")) for m in mods), mods


@pytest.mark.parametrize("check,tamper", [
    ("REFERENCE_WINDOW", lambda c: c["features"].update(reference_end_date=c["signal_date"])),            # the reference reaching T
    ("REFERENCE_WINDOW", lambda c: c["features"].update(breakout_reference=c["features"]["breakout_reference"] * 0.9)),
    ("BREAKOUT_EVENT", lambda c: c["features"].update(breakout_strength=c["features"]["breakout_strength"] + 0.2)),
    ("NO_FUTURE_DATA", lambda c: c["feature_as_of"].update(breakout_strength="2099-01-01")),
    ("ELIGIBILITY", lambda c: c["features"].update(eligible_set_sha="0" * 16)),
    ("CANDIDATE_ACCOUNTING", lambda c: c["features"].update(breakout_candidate_count=c["features"]["breakout_candidate_count"] + 3)),
    ("RANK_SELECTION", lambda c: c["features"].update(rank=c["features"]["rank"] + 1)),
    ("TRANSACTION_FLAGS", lambda c: c.update(entry_trade=not c["entry_trade"])),
    ("EXIT_AT_REBALANCE", lambda c: c.update(max_hold_bars=c["max_hold_bars"] + 2)),
])
def test_U02_a_tampered_record_is_caught(check, tamper):
    ds, run = full_run()
    rec = rec_of(run)
    victim = next(c for c in rec["candidates"] if c["signal_date"] == busy_T(ds))
    tamper(victim)
    assert check in {v["check"] for v in BOA.audit(rec, ds, max_holdings=5, lookback=20) if v["candidate_id"] in (victim["candidate_id"], None)}


def test_U03_a_non_breakout_pick_a_renormalized_weight_and_a_wrong_fill_are_caught():
    ds, run = full_run()
    rec = rec_of(run)
    T = busy_T(ds)
    out = evaluate(ds, T)
    legs = [c for c in rec["candidates"] if c["signal_date"] == T]
    legs[0]["symbol"] = next(s for s in out["eligible"] if s not in [x["symbol"] for x in out["ranked"]])   # an eligible stock that did NOT break out
    assert any(v["check"] == "RANK_SELECTION" and v["candidate_id"] is None for v in BOA.audit(rec, ds, max_holdings=5, lookback=20))
    ds7, run7 = sparse_run()
    rec7 = rec_of(run7)
    t = rec7["arms"]["RULES"]["trades"][0]
    t["qty"] = t["qty"] * 3                                                                           # 7 holdings scaled toward 100 % invested
    t["entry_raw"] = t["entry_raw"] * 1.01
    checks = {v["check"] for v in BOA.audit(rec7, ds7, max_holdings=20, lookback=20) if v["candidate_id"] == t["candidate_id"]}
    assert {"CASH_ALLOCATION", "ENTRY_NEXT_OPEN"} <= checks
    rec7b = rec_of(run7)
    rec7b["config_snapshot"]["sizing"]["position_pct"] = 100.0 / 7
    assert "CASH_ALLOCATION" in {v["check"] for v in BOA.audit(rec7b, ds7, max_holdings=20, lookback=20)}


def test_U04_an_unabsorbed_split_anywhere_is_a_violation_and_the_audit_agrees_with_the_core():
    cos = F.universe(25)
    cos[3]["split"] = (D[300], 4.0)
    sds = F.dataset(cos)
    assert [x for x in BOA.audit({"candidates": []}, sds, max_holdings=5, lookback=20) if x["check"] == "SPLIT_CONSISTENCY"] == []
    bad = rebuild(sds, fn_bar=lambda s, dd, v: v * (4.0 if (s == "ACN" and dd >= D[300]) else 1.0))
    hits = [x for x in BOA.audit({"candidates": []}, bad, max_holdings=5, lookback=20) if x["check"] == "SPLIT_CONSISTENCY"]
    assert hits and "ACN" in hits[0]["detail"]
    T = next(t for t in SCHED if t >= D[300])
    o = evaluate(bad, T)
    assert "ACN" not in o["eligible"] and set(BOA.RawData(bad).expected(T, 20)) == set(o["eligible"])


# ================================================================ G gate, registry, Lab, CLI, isolation =============================================

def test_G01_the_run_is_eligible_persists_and_a_tampered_record_is_blocked(tmp_path, monkeypatch):
    from strategy_validation.store import StrategyValidationStore
    ds, run = full_run()
    rec = rec_of(run)
    assert IG.evaluate(rec).status == "ELIGIBLE" and {c.check: c.status for c in IG.evaluate(rec).checks}["UNIVERSE_INTEGRITY"] == "PASS"
    monkeypatch.setenv("STOCK_AGENT_ENV", "test")
    store = StrategyValidationStore(tmp_path / "v.db")
    store.insert(run)
    StrategyValidationStore(tmp_path / "cash.db").insert(cash_run()[1])                              # an all-cash run persists too (default params)
    got = store.latest("BREAKOUT")
    assert got["cross_sectional"]["control"]["strategy_capital"]["basis"] == "FIXED_SLOTS_CASH" and got["universe_integrity"]["status"] == "UNTRUSTED"
    rec["candidates"][0]["features"]["breakout_strength"] += 1.0
    rec["audit"]["violations"] = BOA.audit(rec, ds, max_holdings=5, lookback=20)
    rec["audit"]["status"] = "FAIL" if rec["audit"]["violations"] else "PASS"
    assert IG.evaluate(rec).status == "BLOCKED"


def test_G02_registry_entry_is_an_implemented_research_strategy_that_never_trades():
    from strategy_registry import service as S
    from strategy_registry.standard import check_signal
    from test_product_flow_612 import signal
    e = R.get("BREAKOUT")
    assert e.implemented and e.status == "RESEARCH" and e.version == BO.STRATEGY_VERSION and e.direction_capability == "LONG" and e.time_horizon == "SWING"
    assert e.source == "INTERNAL" and e.setup_type is None and not e.placeholder_note and R.PLACEHOLDER_IDS == ()
    assert [s["strategy_id"] for s in S.registry_view({"available": False})["strategies"] if s["active"]] == ["DOWNSIDE_DISLOCATION_RECOVERY"]
    assert check_signal(signal(strategy_id="BREAKOUT"), R.by_id())["error"]["code"] == "STRATEGY_INACTIVE"
    assert not re.search(r"(?i)broker|account|quantity|order", json.dumps(e.model_dump()))


def test_G03_the_strategy_lab_detail_has_the_thirteen_sections_and_no_invented_result():
    from strategy_registry import service as S
    d = S.detail_view("BREAKOUT", {"available": False}, validation={"validation_status": "NOT_RUN"})
    assert [s["key"] for s in d["sections"]] == ["overview", "hypothesis", "rules", "data_requirements", "entry_logic", "invalidation", "exit_logic", "holding_horizon",
                                                 "backtest", "out_of_sample", "risk_notes", "paper_results", "decision_log"]
    st = {s["key"]: s for s in d["sections"]}
    f = st["overview"]["facts"]
    assert (f["lookback_sessions"], f["max_holdings"], f["slot_pct"], f["cash_return"], f["universe_method"]) == (20, 20, 5.0, 0.0, "FIXED_CURRENT_UNIVERSE")
    assert d["links"] == {"design_docs": "DESIGN_67_BREAKOUT.md"} and "SURVIVORSHIP_BIAS_PRESENT" in " ".join(st["risk_notes"]["items"])
    assert "BEFORE" in " ".join(st["rules"]["items"]) and "cash" in " ".join(st["rules"]["items"])
    assert not re.search(r"\d+(\.\d+)?\s?%", json.dumps(d["sections"])) and "Sharpe" not in json.dumps(d["sections"])
    assert not re.search(r"requests|urllib|socket|sqlite3|broker|submit|order\(", now_text("strategy_registry/breakout.py"))


def test_G04_cli_validates_and_persists_breakout(tmp_path, monkeypatch):
    from strategy_validation import cli
    from strategy_validation.dataset import dump
    monkeypatch.setenv("STOCK_AGENT_ENV", "test")
    p = tmp_path / "b.json"
    dump(base_ds(), str(p))
    out = io.StringIO()
    code = cli.main(["validate", "--strategy", "BREAKOUT", "--dataset", str(p), "--db", str(tmp_path / "v.db"), "--persist", "--output", str(tmp_path / "r.json")], out=out)
    t = out.getvalue()
    assert code == 0 and t.startswith("BREAKOUT v1.0:") and "selection edge (vs Equal-weight eligible universe):" in t and "universe: FIXED_CURRENT_UNIVERSE -> UNTRUSTED" in t
    assert "stored run" in t, t
    rec = json.loads((tmp_path / "r.json").read_text(encoding="utf-8"))
    assert rec["config_snapshot"]["adapter"]["params"] == {} and rec["cross_sectional"]["control"]["strategy_capital"]["slots"] == 20


def test_G05_a_replay_opens_no_socket_and_imports_no_broker(monkeypatch):
    def refuse(*a, **k):
        raise AssertionError("network access during a replay")
    monkeypatch.setattr(socket, "socket", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)
    assert E.run_validation(BreakoutAdapter({"max_holdings": 5}), F.dataset(F.universe(25)), registry=R.by_id(), created_at=AT).candidate_count > 0
    for f in ("__init__.py", "adapter.py", "audit.py", "core.py", "data.py", "diagnostics.py"):
        tree = ast.parse(now_text(f"breakout/{f}"))
        mods = {n.module or "" for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)} | {a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
        assert not any(m.startswith(("execution", "safety", "paper", "portfolio", "proposals", "api", "requests", "urllib", "httpx", "socket", "alpaca", "anthropic",
                                     "value_quality")) for m in mods), (f, mods)


def test_G08_frozen_controls_and_ci_manifest():
    import execution as X
    import forward_validation as FV
    assert FV.frozen_hash() == FROZEN_HASH and X.MODES == ("MONITOR_ONLY", "PAPER_AUTO")
    assert "test_breakout_67.py" in now_text(".github/ci/pytest_groups.json")
    import mean_reversion as MR
    assert (BO.SPLIT_SESSION, BO.NORMAL_DAY, BO.SPLIT_DAY_TOL, BO.ADJUSTED_LOSS_TOL) == (MR.SPLIT_SESSION, MR.NORMAL_DAY, MR.SPLIT_DAY_TOL, MR.ADJUSTED_LOSS_TOL)
    assert BO.LARGE_REBASING_TOL == 0.05


def test_G06_the_rules_and_the_cash_basis_are_the_frozen_ones():
    r = subprocess.run(["git", "show", f"{FREEZE}:breakout/__init__.py"], cwd=ROOT, capture_output=True, text=True, encoding="utf-8")
    if r.returncode != 0:
        pytest.skip("the freeze commit is not reachable (shallow clone)")
    frozen: dict = {}
    exec(compile(r.stdout, "frozen", "exec"), frozen)                                              # noqa: S102 - the repository's own file at a fixed commit
    for k in ("STRATEGY_VERSION", "RESEARCH_UNIVERSE", "MIN_PRICE", "MIN_MEDIAN_DOLLAR_VOLUME", "DOLLAR_VOLUME_SESSIONS", "LOOKBACK_SESSIONS", "SPLIT_SESSION",
              "NORMAL_DAY", "SPLIT_DAY_TOL", "ADJUSTED_LOSS_TOL", "LARGE_REBASING_TOL", "MAX_HOLDINGS", "SLOT_PCT", "CASH_RETURN", "REBALANCE",
              "FRICTION_BPS_PER_SIDE", "SENSITIVITY", "FOLLOW_THROUGH_BAND", "POSITION_BUCKETS", "CONCENTRATION_TOP_NAMES", "CONCENTRATION_NAME_SHARE"):
        assert frozen[k] == getattr(BO, k), k                                                      # nothing tuned after the OOS result
    for f in ("breakout/core.py", "breakout/data.py", "breakout/adapter.py"):
        assert subprocess.run(["git", "diff", "--quiet", FREEZE, "--", f], cwd=ROOT).returncode == 0, f   # the rule code is byte-identical
    v3 = subprocess.run(["git", "show", f"{FREEZE}:strategy_validation/cross_sectional.py"], cwd=ROOT, capture_output=True, text=True, encoding="utf-8").stdout
    assert "CASH_DRIVEN_EXCESS" in v3 and "FIXED_SLOTS_CASH" in v3                                   # the cash basis and its guard were pre-registered


def test_G07_documents_state_the_hypothesis_the_result_and_the_limits():
    d = now_text("DESIGN_67_BREAKOUT.md")
    for s in ("## 1. Hypothesis (written before any code)", "## 2. Definition (v1, frozen before the OOS run)", "## 3. Feasibility", "## 5. Results",
              "SURVIVORSHIP_BIAS_PRESENT", "FIXED_CURRENT_UNIVERSE", "UNTRUSTED", "NOT_ESTABLISHED", "SELECTION CONTROL", "MARKET BENCHMARK",
              "NOT SELECTION-EDGE EVIDENCE", "## 6a. Development log", "## 7. Adversarial review", "## 8. Limitations", "Backtest results are hypothetical.",
              FREEZE, "CASH_DRIVEN_EXCESS", "never clipped", "invested fraction"):
        assert s in d, s
    h = now_text("STRATEGY_VALIDATION_HARNESS.md")
    assert "## 16. Fixed-slot cash accounting (Stage 6.7, opt-in)" in h and "CASH_DRIVEN_EXCESS" in h
    readme = now_text("README.md")
    sec = re.findall(r"(?ms)^## Breakout strategy \(Stage 6\.7, research only\)\n.*?(?=^## )", readme)
    assert len(sec) == 1 and "python -m strategy_validation validate --strategy BREAKOUT" in sec[0] and "SURVIVORSHIP_BIAS_PRESENT" in sec[0]
    assert readme.index("## Mean Reversion strategy (Stage 6.6") < readme.index("## Breakout strategy (Stage 6.7") < readme.index("## Continuous integration (Stage 6.0.3")
    assert not re.search(r"(?i)\b(guaranteed|profitable|buy now|sell now|regime)\b", sec[0])


def test_G09_the_real_breakout_result_where_the_local_data_exists():
    db = ROOT / "runtime" / "preview_67.db"
    if not db.exists():
        pytest.skip("local data only: the Breakout run lives in a git-ignored runtime database")
    from strategy_validation.store import StrategyValidationStore
    store = StrategyValidationStore(db)
    run = store.latest("BREAKOUT")
    if run is None:
        pytest.skip("no stored Breakout run in this local database")
    cs, oo = run["cross_sectional"], run["cross_sectional"]["segments"]["OOS"]
    assert run["validation_version"] == "validation_v3" and run["eligibility"]["status"] == "ELIGIBLE" and cs["parity"]["status"] == "PASS" and run["audit"]["status"] == "PASS"
    assert run["candidate_set_hash"] == FREEZE_RUN_SET and cs["control"]["strategy_capital"] == {"basis": "FIXED_SLOTS_CASH", "slots": 20, "slot_pct": 5.0, "cash_return": 0.0}
    assert cs["selection_edge"]["status"] == run["selection_edge"]["status"] == "NOT_ESTABLISHED" and oo["period_count"] == 98 and oo["excess_t_stat"] < 1.0
    assert run["verdict"]["verdict"] == "FAIL" and run["performance_verdict"]["verdict"] == "PASS" and run["universe_integrity"]["status"] == "UNTRUSTED"
    assert 0.2 < oo["mean_invested_fraction"] < 0.5 and "SURVIVORSHIP_BIAS_PRESENT" in run["research_flags"] and R.get("BREAKOUT").status == "RESEARCH"
    first = [StrategyValidationStore(db).run(x["run_id"]) for x in store.runs("BREAKOUT")]
    assert any((x or {}).get("git_commit_sha", "").startswith(FREEZE) for x in first)                  # the first OOS run ran the frozen code
