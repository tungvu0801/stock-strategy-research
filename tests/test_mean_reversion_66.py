"""Stage 6.6 — Strategy #4: Mean Reversion v1 (mean_reversion/, strategy_registry/mean_reversion.py, DESIGN_66_MEAN_REVERSION.md).

  S  the signal: the deterministic 5-session return, the ranking DIRECTION, no future data, splits, the provider artifact, missing history and bars
  W  the weekly calendar: a normal week, a Monday holiday, a shortened week, a year boundary - one decision per ISO week
  E  eligibility and ranking: price, liquidity, one listing per issuer, ties, the N most negative, equal weight
  L  the adapter: weekly legs, next-open entry, next-rebalance exit, ENTER / HOLD / EXIT flags, friction, candidate stability and identity
  V  validation_v3 + universe_integrity_v1: the control, one observation per week, turnover, sensitivity on TRAIN only, concentration, the cap,
     the falling-knife / market-week / Momentum-overlap / data-quality diagnostics
  U  the independent audit (it re-derives the selection; tampered records are caught; it never imports the strategy's code)
  G  the integrity gate, the registry, the Strategy Lab, the CLI, isolation, frozen controls

Fixtures are SYNTHETIC (tests/mean_reversion_fixtures.py). The real run is reproduced by `python -m strategy_validation validate --strategy
MEAN_REVERSION --dataset runtime/vq/vq_bundle.json`."""
from __future__ import annotations

import ast
import copy
import functools
import io
import json
import re
import socket
import subprocess
import sys
from datetime import date
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

import mean_reversion as MR  # noqa: E402
import mean_reversion_fixtures as F  # noqa: E402
from mean_reversion import audit as MRA  # noqa: E402
from mean_reversion.adapter import MeanReversionAdapter, eligible_set_sha  # noqa: E402
from mean_reversion.core import INELIGIBLE, MeanReversionStrategyCore, iso_week, pct_ranks  # noqa: E402
from mean_reversion.data import PriceData  # noqa: E402
from strategy_registry import registry as R  # noqa: E402
from strategy_validation import engine as E  # noqa: E402
from strategy_validation import integrity as IG  # noqa: E402
from strategy_validation.dataset import Dataset  # noqa: E402

AT = "2026-10-26T00:00:00+00:00"
FROZEN_HASH = "b5d4a56ef5bba61f8250b3cd25182cc1655738adf0605a77671fd029883f8530"
FREEZE = "626fb11"                                                                              # "freeze Mean Reversion v1 rules before OOS"
D = F.weekdays("2019-01-02", "2022-12-30")


def now_text(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8").replace("\r\n", "\n")


@functools.lru_cache(maxsize=None)
def base_ds():
    return F.dataset(F.universe(25))


@functools.lru_cache(maxsize=None)
def rev_ds():
    return F.dataset(F.universe(25), shock=0.06)                                                    # a designed weekly reversal


@functools.lru_cache(maxsize=None)
def full_run():
    ds = base_ds()
    return ds, E.run_validation(MeanReversionAdapter({"top_n": 5}), ds, registry=R.by_id(), created_at=AT)


@functools.lru_cache(maxsize=None)
def rev_run():
    ds = rev_ds()
    return ds, E.run_validation(MeanReversionAdapter({"top_n": 5}), ds, registry=R.by_id(), created_at=AT)


def rec_of(run) -> dict:
    return json.loads(run.model_dump_json())


def evaluate(ds, T, top_n=5, **kw):
    data = PriceData(ds)
    syms = [s for s in MR.RESEARCH_UNIVERSE if s in ds.bars]
    return MeanReversionStrategyCore(top_n=top_n, **kw).evaluate(T, syms, data)


def rebuild(ds, fn_bar=None, fn_raw=None):
    """A new dataset with every bar / raw close passed through fn(symbol, date, value) (a deep copy: the original is untouched)."""
    bars = {s: [{"date": b.date, **{k: (fn_bar(s, b.date, getattr(b, k)) if fn_bar else getattr(b, k)) for k in ("open", "high", "low", "close")}, "volume": b.volume}
                for b in rows] for s, rows in ds.bars.items()}
    aux = copy.deepcopy(ds.aux)
    if fn_raw:
        for s, m in aux["series"]["raw_close"].items():
            for d in m:
                m[d] = fn_raw(s, d, m[d])
    return Dataset(bars, kind=ds.kind, source=ds.source, provenance=ds.declared, aux=aux)


def T_mid(ds, k=40):
    return MeanReversionStrategyCore().schedule(ds.sessions)[k]


def snap(sym, ret):
    return {"symbol": sym, "eligible": True, "reason": None, "recent_return": ret, "inputs": {}, "anomaly": None}


# ================================================================ S the signal =====================================================================

def test_S01_the_signal_is_the_5_session_adjusted_return_through_T_close():
    ds = base_ds()
    T = T_mid(ds)
    i = ds.sessions.index(T)
    out = evaluate(ds, T)
    x = out["ranked"][0]
    inp = x["snapshot"]["inputs"]
    assert (inp["lookback_start_date"], inp["lookback_end_date"]) == (ds.sessions[i - 5], T)
    closes = {b.date: b.close for b in ds.bars[x["symbol"]]}
    assert x["recent_return"] == pytest.approx(closes[T] / closes[ds.sessions[i - 5]] - 1.0)
    assert MR.LOOKBACK_SESSIONS == 5 and evaluate(ds, T)["ranked"] == out["ranked"]                  # deterministic


def test_S02_the_most_negative_recent_return_ranks_first_never_momentum():
    core = MeanReversionStrategyCore(top_n=2)
    ranked = core.rank([snap("D", 0.05), snap("B", -0.10), snap("C", -0.02), snap("A", -0.15)])
    assert [x["symbol"] for x in ranked] == ["A", "B", "C", "D"] and [x["rank"] for x in ranked] == [1, 2, 3, 4]
    assert [x["percentile"] for x in ranked] == [1.0, 0.75, 0.5, 0.25]                                  # the score: most negative = highest
    out = evaluate(base_ds(), T_mid(base_ds()), top_n=5)
    rets = sorted(x["recent_return"] for x in out["ranked"])
    assert sorted(x["recent_return"] for x in out["ranked"] if x["symbol"] in out["selected"]) == rets[:5]   # the 5 LOWEST returns
    worst = min(out["ranked"], key=lambda x: x["recent_return"])
    assert worst["rank"] == 1 and worst["percentile"] == 1.0 and worst["symbol"] in out["selected"]


def test_S03_rewriting_every_price_after_T_leaves_the_signal_ranks_and_portfolio_byte_identical():
    ds = base_ds()
    T = T_mid(ds)
    future = rebuild(ds, fn_bar=lambda s, d, v: v * (0.1 if d > T else 1.0) * (7.0 if (d > T and s in ("AAPL", "ABBV")) else 1.0),
                     fn_raw=lambda s, d, v: v * (0.1 if d > T else 1.0))
    a, b = evaluate(ds, T), evaluate(future, T)
    key = lambda o: json.dumps([(x["symbol"], x["recent_return"], x["percentile"], x["rank"]) for x in o["ranked"]] + [o["selected"]], sort_keys=True)  # noqa: E731
    assert key(a) == key(b)
    ca = [c for c in MeanReversionAdapter({"top_n": 5}).candidates(ds) if c.signal_date == T]
    cb = [c for c in MeanReversionAdapter({"top_n": 5}).candidates(future) if c.signal_date == T]
    assert json.dumps([(c.symbol, c.score, c.features["recent_return"], c.features["rank"]) for c in ca]) == \
        json.dumps([(c.symbol, c.score, c.features["recent_return"], c.features["rank"]) for c in cb])


def test_S04_a_split_never_creates_a_fake_loser():
    plain = F.universe(25)
    split = copy.deepcopy(plain)
    split[3]["split"] = (D[300], 4.0)                                                               # ACN splits 4-for-1: the raw price falls 75 %
    a, b = F.dataset(plain), F.dataset(split)
    core = MeanReversionStrategyCore(top_n=5)
    for T in [t for t in core.schedule(a.sessions) if D[290] <= t <= D[320]]:                      # every window around the split
        ea, eb = evaluate(a, T), evaluate(b, T)
        assert [x["symbol"] for x in ea["ranked"]] == [x["symbol"] for x in eb["ranked"]] and ea["selected"] == eb["selected"], T
        assert all(x["recent_return"] == pytest.approx(y["recent_return"], rel=1e-6, abs=1e-6) for x, y in zip(ea["ranked"], eb["ranked"]))
        acn = next(x for x in eb["ranked"] if x["symbol"] == "ACN")
        assert acn["recent_return"] > -0.2                                                          # never the -75 % the raw price shows
    assert b.aux["series"]["raw_close"]["ACN"][D[300]] < b.aux["series"]["raw_close"]["ACN"][D[299]] / 3


def test_S05_a_hon_type_provider_artifact_cannot_become_a_mean_reversion_winner():
    cos = F.universe(25)
    k = next(i for i, c in enumerate(cos) if c["symbol"] == "HON")
    cos[k]["ratio_steps"] = [(D[300], 0.5)]                                                         # the ratio moves 0.5 -> 1.0, the raw price does not
    cos[k]["adj_steps"] = [(D[300], 1.048)]                                                          # ... and the adjusted series steps down ~4.6 %
    ds = F.dataset(cos)
    T = next(t for t in MeanReversionStrategyCore().schedule(ds.sessions) if t >= D[300])           # the window contains the artifact
    out = evaluate(ds, T)
    ex = {x["symbol"]: x for x in out["data_exclusions"]}
    assert "HON" not in [x["symbol"] for x in out["ranked"]] and "HON" not in out["selected"] and ex["HON"]["code"] == "RATIO_ARTIFACT"
    assert out["summary"]["ineligible_by_reason"]["DATA_INCONSISTENT"] == 1 and "DATA_INCONSISTENT" in INELIGIBLE
    assert evaluate(ds, T) == out                                                                   # deterministic
    only_adj = F.universe(25)
    only_adj[k]["adj_steps"] = [(D[300], 1.06)]                                                      # an adjusted loss the raw price did not have
    o2 = evaluate(F.dataset(only_adj), T)
    assert "HON" not in o2["selected"] and {x["symbol"]: x["code"] for x in o2["data_exclusions"]} == {"HON": "ADJUSTED_LOSS_WITHOUT_RAW_MOVE"}
    later = next(t for t in MeanReversionStrategyCore().schedule(ds.sessions) if ds.sessions.index(t) - 5 > ds.sessions.index(D[300]))
    assert "HON" in [x["symbol"] for x in evaluate(ds, later)["ranked"]]                             # only windows containing the event are excluded
    snaps = MeanReversionStrategyCore().snapshot("HON", T, PriceData(ds))
    assert snaps["recent_return"] is None and snaps["anomaly"]["from"] < snaps["anomaly"]["to"] <= T  # flagged, never clipped into a return


def test_S06_a_missing_history_or_endpoint_bar_is_excluded_never_filled():
    cos = F.universe(25)
    cos[5]["start"] = D[200]                                                                         # ADI listed later
    ds = F.dataset(cos)
    T = next(t for t in MeanReversionStrategyCore().schedule(ds.sessions) if ds.sessions.index(t) >= ds.sessions.index(D[200]) + 1
             and ds.sessions.index(t) - 5 < ds.sessions.index(D[200]))
    out = evaluate(ds, T)
    assert "ADI" not in [x["symbol"] for x in out["ranked"]] and out["summary"]["ineligible_by_reason"] == {"LIQUIDITY_HISTORY": 1}   # 20-session history first
    T2 = T_mid(base_ds())
    i2 = D.index(T2)
    for gap, reason in ((D[i2 - 5], "MISSING_ENDPOINT_BAR"), (T2, "NO_PRICE")):
        g = F.universe(25)
        g[6]["gaps"] = [gap]                                                                         # AMAT has no bar on one endpoint
        o = evaluate(F.dataset(g), T2)
        assert "AMAT" not in [x["symbol"] for x in o["ranked"]] and o["summary"]["ineligible_by_reason"][reason] == 1, reason
    inside = F.universe(25)
    inside[6]["gaps"] = [D[i2 - 2]]                                                                  # a hole inside the window: the endpoints decide
    o3 = evaluate(F.dataset(inside), T2)
    x = next(x for x in o3["ranked"] if x["symbol"] == "AMAT")
    closes = {b.date: b.close for b in F.dataset(inside).bars["AMAT"]}
    assert x["recent_return"] == pytest.approx(closes[T2] / closes[D[i2 - 5]] - 1.0)


# ================================================================ W the weekly calendar ==============================================================

def test_W01_one_decision_per_iso_week_monday_holiday_short_week_and_year_boundary():
    hol = ("2024-01-01", "2024-01-15", "2024-11-28", "2024-11-29", "2024-12-25", "2025-01-01")
    sessions = F.weekdays("2023-11-01", "2025-01-31", hol)
    sched = MeanReversionStrategyCore().schedule(sessions)
    weeks = [iso_week(s) for s in sched]
    assert len(weeks) == len(set(weeks))                                                             # exactly one per week
    first = {}
    for i, s in enumerate(sessions):
        first.setdefault(iso_week(s), i)
    assert set(weeks) == {w for w, i in first.items() if i >= 20}                                    # every week whose first session has the history
    assert "2024-01-16" in sched and "2024-01-15" not in sched                                        # a Monday holiday: the Tuesday
    assert "2024-01-02" in sched                                                                     # New Year's Day Monday: the Tuesday
    assert "2024-11-25" in sched and all(not s.startswith("2024-11-2") or s == "2024-11-25" for s in sched)   # a shortened (Thanksgiving) week: one
    assert "2024-12-30" in sched and "2025-01-02" not in sched and "2025-01-06" in sched               # 2024-12-30 .. 2025-01-03 is ONE ISO week
    assert sched == MRA.schedule(sessions, MR.LOOKBACK_SESSIONS)                                       # the audit's independent calendar agrees


# ================================================================ E eligibility + ranking ===========================================================

def test_E01_price_and_liquidity_filters_and_one_listing_per_issuer():
    cos = F.universe(25)
    cos[0]["price"] = 2.0                                                                            # AAPL below 5 USD (raw)
    cos[1]["volume"] = 1000.0                                                                        # ABBV illiquid
    cos[2]["cik"] = cos[3]["cik"]                                                                    # ABT and ACN: one issuer -> ACN is a second listing
    out = evaluate(F.dataset(cos), T_mid(base_ds()))
    r = out["summary"]["ineligible_by_reason"]
    assert r == {"PRICE_BELOW_MIN": 1, "LIQUIDITY_BELOW_MIN": 1, "SECOND_LISTING": 1} and set(r) <= set(INELIGIBLE)
    assert {"AAPL", "ABBV", "ACN"}.isdisjoint(x["symbol"] for x in out["ranked"]) and "ABT" in [x["symbol"] for x in out["ranked"]]


def test_E02_ties_rank_by_symbol_and_percentiles_are_tie_averaged():
    assert pct_ranks([("B", 0.1), ("A", 0.1), ("C", 0.3)]) == {"A": 0.5, "B": 0.5, "C": 1.0}
    ranked = MeanReversionStrategyCore(top_n=2).rank([snap("C", -0.1), snap("A", -0.1), snap("B", -0.1), snap("D", 0.2)])
    assert [x["symbol"] for x in ranked] == ["A", "B", "C", "D"] and ranked[0]["percentile"] == ranked[2]["percentile"] == pytest.approx(3.0 / 4)


def test_E03_the_n_most_negative_at_equal_weight():
    assert (MR.TOP_N, MR.POSITION_PCT) == (20, 5.0) and MeanReversionStrategyCore().top_n == 20
    a = MeanReversionAdapter()
    assert a.sizing == {"mode": "EQUAL_WEIGHT", "position_pct": 5.0} and a.stop_policy == "NONE" and MeanReversionAdapter({"top_n": 5}).sizing["position_pct"] == 20.0
    out = evaluate(base_ds(), T_mid(base_ds()), top_n=5)
    assert len(out["selected"]) == 5 and [x["rank"] for x in out["ranked"]] == list(range(1, len(out["ranked"]) + 1))


# ================================================================ L the adapter =====================================================================

def test_L01_weekly_legs_enter_at_the_next_open_and_exit_at_the_open_after_the_next_rebalance():
    ds, run = full_run()
    sched = MeanReversionStrategyCore().schedule(ds.sessions)
    trades = {t.candidate_id: t for t in run.arms["RULES"].trades}
    seen = 0
    for c in run.candidates[:200]:
        t = trades.get(c.candidate_id)
        if t is None:
            continue
        seen += 1
        k = sched.index(c.signal_date)
        assert t.entry_date == ds.sessions[ds.sessions.index(c.signal_date) + 1] and t.entry_raw == ds.bar(c.symbol, t.entry_date).open
        assert t.entry_date > c.signal_date                                                          # signal information time < entry execution time
        if k + 1 < len(sched):
            assert t.exit_date == ds.sessions[ds.sessions.index(sched[k + 1]) + 1] and t.exit_raw == ds.bar(c.symbol, t.exit_date).open and t.exit_reason == "TIME_EXIT"
    assert seen > 100


def test_L02_friction_only_on_real_orders_and_never_on_a_continuation():
    ds, run = full_run()
    slip = run.friction_model["slippage_bps_per_side"] / 10000.0
    assert slip == MR.FRICTION_BPS_PER_SIDE / 10000.0
    legs = {c.candidate_id: c for c in run.candidates}
    holds = 0
    for t in run.arms["RULES"].trades:
        c = legs[t.candidate_id]
        assert t.entry_fill == pytest.approx(t.entry_raw * (1 + slip) if c.entry_trade else t.entry_raw)
        assert t.exit_fill == pytest.approx(t.exit_raw * (1 - slip) if c.exit_trade else t.exit_raw)
        holds += not c.entry_trade
    assert holds > 0 and run.execution_assumptions["stop_policy"] == "NONE" and run.execution_assumptions["time_exit_fill"] == "OPEN"


def test_L03_candidates_are_stable_identical_across_runs_and_every_arm_gets_the_same_set():
    ds, run = full_run()
    again = E.run_validation(MeanReversionAdapter({"top_n": 5}), F.dataset(F.universe(25)), registry=R.by_id(), created_at=AT)
    assert run.candidate_set_hash == again.candidate_set_hash and [c.candidate_id for c in run.candidates] == [c.candidate_id for c in again.candidates]
    assert run.cross_sectional["segments"] == again.cross_sectional["segments"]
    assert run.integrity["same_candidate_set"] and run.arms["GATED"].metrics == run.arms["RULES"].metrics and run.arms["MODEL"].status == "NOT_EVALUATED"
    assert len({(c.symbol, c.signal_date) for c in run.candidates}) == run.candidate_count            # one candidate per stock-week


# ================================================================ V validation_v3 + universe_integrity_v1 ===========================================

def test_V01_the_control_is_the_same_eligible_universe_and_every_parity_check_passes():
    ds, run = full_run()
    cs = run.cross_sectional
    assert run.evaluation_mode == "CROSS_SECTIONAL_PORTFOLIO" and cs["control"]["control_id"] == "EQUAL_WEIGHT_ELIGIBLE_UNIVERSE"
    assert cs["control"]["role"] == "SELECTION_CONTROL" and cs["market_benchmark"]["role"] == "MARKET_BENCHMARK" and cs["market_benchmark"]["symbol"] == "SPY"
    assert cs["parity"]["status"] == "PASS" and len(cs["parity"]["checks"]) == 8
    snaps = {s["rebalance"]: s for s in MeanReversionAdapter({"top_n": 5}).selection_snapshots(ds)}
    for c in run.candidates:
        assert c.features["eligible_set_sha"] == eligible_set_sha(snaps[c.signal_date]["eligible"]) and c.features["eligible_count"] == len(snaps[c.signal_date]["eligible"])
    assert run.diagnostics["strategy"]["control_membership"]["mismatches"] == 0


def test_V02_one_observation_per_week_and_the_boundary_on_both_sides():
    ds, run = full_run()
    cs = run.cross_sectional
    inc = [p for p in cs["periods"] if p["status"] == "INCLUDED"]
    assert all(p["strategy"]["positions"] == 5 for p in inc) and cs["segments"]["ALL"]["period_count"] == len(inc)
    assert cs["excluded_periods"] == {"BOUNDARY": 1} and run.arms["RULES"].metrics["ALL"]["trade_count"] == 5 * len(inc)
    assert 50 < cs["periods_per_year"] < 54 and len({iso_week(p["rebalance"]) for p in cs["periods"]}) == len(cs["periods"])


def test_V03_turnover_friction_and_absolute_performance_diagnostics():
    ds, run = full_run()
    d = run.diagnostics["strategy"]
    log = MeanReversionAdapter({"top_n": 5})
    list(log.candidates(ds))
    rl = log.rebalance_log[ds.dataset_id]
    assert d["construction"]["average_one_way_turnover"] == pytest.approx(sum(len(r["enter"]) for r in rl[1:]) / 5 / (len(rl) - 1), abs=1e-4)
    assert d["construction"]["annualized_one_way_turnover"] == pytest.approx(d["construction"]["average_one_way_turnover"] * run.cross_sectional["periods_per_year"], rel=1e-3)
    f = d["friction"]["ALL"]
    assert f["mean_excess_gross"] >= f["mean_excess_net"] and "friction_consumes_gross_excess" in f
    p = d["portfolio"]["ALL"]
    assert set(p) == {"strategy", "selection_control", "market_benchmark"} and {"total_return", "volatility_annualized", "sharpe_annualized", "max_drawdown"} <= set(p["strategy"])
    seg = run.cross_sectional["segments"]["ALL"]
    assert p["strategy"]["total_return"] == pytest.approx(seg["strategy_total_return"], abs=1e-5) and p["selection_control"]["total_return"] == pytest.approx(seg["control_total_return"], abs=1e-5)


def test_V04_the_sensitivity_never_sees_a_bar_after_the_train_end():
    ds, run = full_run()
    s = run.diagnostics["strategy"]["sensitivity"]
    assert [r["params"] for r in s["rows"]] == ["base"] + list(MR.SENSITIVITY) and "TRAIN view only" in s["scope"] and len({r["periods"] for r in s["rows"]}) == 1
    assert MR.SENSITIVITY == ({"lookback_sessions": 3}, {"lookback_sessions": 10}, {"top_n": 15}, {"top_n": 25})
    wrecked = rebuild(ds, fn_bar=lambda sym, d, v: v * (3.0 if d > run.train_end else 1.0), fn_raw=lambda sym, d, v: v * (3.0 if d > run.train_end else 1.0))
    from mean_reversion import diagnostics as MRD
    base = {"top_n": 5, "lookback_sessions": 5}
    assert MRD.sensitivity(wrecked, run.train_end, 0.0005, base) == MRD.sensitivity(ds, run.train_end, 0.0005, base)


def test_V05_sector_concentration_and_name_attribution():
    ds, run = rev_run()
    c = run.diagnostics["strategy"]["concentration"]
    assert c["sic_major_group"]["most_frequent_top_groups"] and c["most_held_names"] and c["excess_contribution"]["top"]
    inc = [p for p in run.cross_sectional["periods"] if p["status"] == "INCLUDED"]
    assert c["excess_contribution"]["summed_period_excess"] == pytest.approx(sum(p["excess"] for p in inc), abs=1e-6)
    from mean_reversion import diagnostics as MRD
    total = sum(v for _, v in c["excess_contribution"]["top"])
    assert c["excess_contribution"]["top_five_share_of_summed_excess"] == pytest.approx(total / sum(p["excess"] for p in inc), abs=1e-3)
    assert (MR.FLAG_NAMES in run.research_flags) == (c["excess_contribution"]["top_five_share_of_summed_excess"] >= MR.CONCENTRATION_NAME_SHARE)
    assert MRD.MAJOR_GROUPS["36"].startswith("Electronic")


def test_V06_the_universe_is_declared_fixed_current_and_survivorship_is_flagged():
    ds, run = full_run()
    d = MeanReversionAdapter.universe_declaration
    assert d["universe_method"] == "FIXED_CURRENT_UNIVERSE" and d["survivorship_bias"] == "PRESENT" and d["includes_delisted"] is False
    assert d["coverage_start"] is None and d["coverage_end"] is None
    assert run.universe_integrity["status"] == "UNTRUSTED" and "SURVIVORSHIP_BIAS_PRESENT" in run.research_flags
    import value_quality as VQ
    assert MR.RESEARCH_UNIVERSE == VQ.RESEARCH_UNIVERSE and len(MR.RESEARCH_UNIVERSE) == 81


def test_V07_a_raw_established_edge_is_capped_by_the_untrusted_universe_and_never_passes():
    ds, run = rev_run()
    raw = run.cross_sectional["selection_edge"]
    assert raw["status"] == "ESTABLISHED" and run.cross_sectional["segments"]["OOS"]["excess_t_stat"] >= 2.0     # the designed reversal
    assert run.selection_edge["status"] == "NOT_ESTABLISHED" and run.selection_edge["raw_status"] == "ESTABLISHED" and run.selection_edge["capped"]
    assert run.selection_edge["reasons"][0]["code"] == "UNIVERSE_INTEGRITY_CAP" and run.verdict["verdict"] == "FAIL" and run.performance_verdict["verdict"] == "PASS"
    assert IG.evaluate(rec_of(run)).status == "ELIGIBLE" and R.get("MEAN_REVERSION").status == "RESEARCH"


def test_V08_falling_knife_market_weeks_and_momentum_overlap_are_diagnostics_only():
    ds, run = rev_run()
    d = run.diagnostics["strategy"]
    k = d["falling_knife"]
    assert k["legs"] > 0 and k["share_rebounding"] + k["share_falling_further"] <= 1.0 and set(k["by_depth_of_the_drop"]) == {"deepest_third", "middle_third", "shallowest_third"}
    assert set(d["market_weeks"]["ALL"]) == {"benchmark_up", "benchmark_down"}
    ov = d["momentum_overlap"]
    assert ov["rebalances"] > 0 and 0 <= ov["share_of_selected_in_momentum_bottom_quintile"] <= 1 and "never combined" in ov["note"]
    src = now_text("mean_reversion/core.py") + now_text("mean_reversion/adapter.py") + now_text("mean_reversion/data.py")
    assert not re.search(r"(?m)^\s*(from|import) (momentum|value_quality)", src)                     # the rules never use another strategy


def test_V09_the_data_quality_diagnostics_report_exclusions_and_artifacts():
    cos = F.universe(25)
    k = next(i for i, c in enumerate(cos) if c["symbol"] == "HON")
    cos[k]["ratio_steps"], cos[k]["adj_steps"] = [(D[300], 0.5)], [(D[300], 1.048)]
    ds = F.dataset(cos)
    run = E.run_validation(MeanReversionAdapter({"top_n": 5}), ds, registry=R.by_id(), created_at=AT)
    dq = run.diagnostics["strategy"]["data_quality"]
    assert dq["data_inconsistent_exclusions"] >= 1 and dq["by_code"] == {"RATIO_ARTIFACT": dq["data_inconsistent_exclusions"]}
    assert [a["symbol"] for a in dq["ratio_artifacts"]] == ["HON"] and run.audit.status == "PASS"   # reported, not failed
    assert all(x["symbol"] == "HON" for x in dq["exclusions"])
    held = dq["holding_window_anomalies"]                                                            # the artifact inside HON's held control window
    assert held and {(x["symbol"], x["code"]) for x in held} == {("HON", "RATIO_ARTIFACT")} and any(x["side"] == "control" for x in held)
    x = next(x for x in held if x["side"] == "control")
    assert x["step"] == pytest.approx(1 / 1.048 - 1, abs=1e-4) and x["effect_on_period_excess"] > 0   # a lower control return: a higher excess
    assert "HOLDING_WINDOW_DATA_ANOMALY" in run.research_flags and dq["holding_window_effect_on_summed_excess"] ==         pytest.approx(sum(x["effect_on_period_excess"] for x in held if x["period_included"]), abs=1e-5)
    clean = full_run()[1].diagnostics["strategy"]["data_quality"]
    assert clean["holding_window_anomaly_count"] == 0 and "HOLDING_WINDOW_DATA_ANOMALY" not in full_run()[1].research_flags


# ================================================================ U the independent audit ===========================================================

def test_U01_the_audit_re_derives_the_selection_and_never_imports_the_strategy():
    ds, run = full_run()
    assert MRA.audit(rec_of(run), ds, top_n=5, lookback=5) == [] and run.audit.status == "PASS"
    tree = ast.parse(now_text("mean_reversion/audit.py"))
    mods = {n.module or "" for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)} | {a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
    assert not any(m.startswith(("mean_reversion.core", "mean_reversion.data", "mean_reversion.adapter", "momentum")) for m in mods), mods


@pytest.mark.parametrize("check,tamper", [
    ("REVERSAL_RETURN", lambda c: c["features"].update(recent_return=c["features"]["recent_return"] - 0.5)),
    ("LOOKBACK_ENDPOINTS", lambda c: c["features"].update(lookback_start_date=c["signal_date"])),
    ("NO_FUTURE_DATA", lambda c: c["feature_as_of"].update(recent_return="2099-01-01")),
    ("ELIGIBILITY", lambda c: c["features"].update(eligible_count=c["features"]["eligible_count"] - 3)),
    ("ELIGIBILITY", lambda c: c["features"].update(eligible_set_sha="0" * 16)),
    ("RANK_SELECTION", lambda c: c["features"].update(rank=c["features"]["rank"] + 1)),
    ("RANK_SELECTION", lambda c: c.update(score=c["score"] - 10)),
    ("TRANSACTION_FLAGS", lambda c: c.update(entry_trade=not c["entry_trade"])),
    ("EXIT_AT_REBALANCE", lambda c: c.update(max_hold_bars=c["max_hold_bars"] + 2)),
])
def test_U02_a_tampered_record_is_caught(check, tamper):
    ds, run = full_run()
    rec = rec_of(run)
    victim = next(c for c in rec["candidates"] if c["signal_date"] == MeanReversionStrategyCore().schedule(ds.sessions)[30])
    tamper(victim)
    assert check in {v["check"] for v in MRA.audit(rec, ds, top_n=5, lookback=5) if v["candidate_id"] in (victim["candidate_id"], None)}


def test_U03_a_swapped_selection_a_momentum_selection_wrong_fills_and_wrong_weights_are_caught():
    ds, run = full_run()
    rec = rec_of(run)
    T = MeanReversionStrategyCore().schedule(ds.sessions)[30]
    out = evaluate(ds, T, top_n=5)
    legs = [c for c in rec["candidates"] if c["signal_date"] == T]
    legs[0]["symbol"] = out["ranked"][-1]["symbol"]                                                   # the BEST performer: a momentum pick
    assert any(v["check"] == "RANK_SELECTION" and v["candidate_id"] is None for v in MRA.audit(rec, ds, top_n=5, lookback=5))
    rec2 = rec_of(run)
    t = rec2["arms"]["RULES"]["trades"][10]
    t["entry_raw"] = t["entry_raw"] * 1.01                                                            # not the next open
    t["qty"] = t["qty"] * 3                                                                           # 3x the equal weight
    checks = {v["check"] for v in MRA.audit(rec2, ds, top_n=5, lookback=5) if v["candidate_id"] == t["candidate_id"]}
    assert {"ENTRY_NEXT_OPEN", "POSITION_WEIGHT"} <= checks
    rec3 = rec_of(run)
    rec3["config_snapshot"]["sizing"]["position_pct"] = 10.0
    assert "POSITION_WEIGHT" in {v["check"] for v in MRA.audit(rec3, ds, top_n=5, lookback=5)}


def test_U04_an_unabsorbed_split_anywhere_is_a_violation_and_the_audit_agrees_with_the_core_on_exclusions():
    cos = F.universe(25)
    cos[3]["split"] = (D[300], 4.0)
    sds = F.dataset(cos)
    assert [x for x in MRA.audit({"candidates": []}, sds, top_n=5, lookback=5) if x["check"] == "SPLIT_CONSISTENCY"] == []
    bad = rebuild(sds, fn_bar=lambda s, dd, v: v * (4.0 if (s == "ACN" and dd >= D[300]) else 1.0))   # the split left UN-adjusted
    hits = [x for x in MRA.audit({"candidates": []}, bad, top_n=5, lookback=5) if x["check"] == "SPLIT_CONSISTENCY"]
    assert hits and "ACN" in hits[0]["detail"] and "not absorbed" in hits[0]["detail"]
    T = next(t for t in MeanReversionStrategyCore().schedule(bad.sessions) if t >= D[300])
    assert "ACN" not in evaluate(bad, T)["selected"] and {x["symbol"]: x["code"] for x in evaluate(bad, T)["data_exclusions"]}["ACN"] == "SPLIT_NOT_ABSORBED"
    assert set(MRA.expected_universe(bad, T, 5)) == {x["symbol"] for x in evaluate(bad, T)["ranked"]}   # the audit re-derives the same exclusion


# ================================================================ G gate, registry, Lab, CLI, isolation =============================================

def test_G01_the_run_is_eligible_persists_and_a_tampered_record_is_blocked(tmp_path, monkeypatch):
    from strategy_validation.store import StrategyValidationStore
    ds, run = full_run()
    rec = rec_of(run)
    assert IG.evaluate(rec).status == "ELIGIBLE" and {c.check: c.status for c in IG.evaluate(rec).checks}["UNIVERSE_INTEGRITY"] == "PASS"
    monkeypatch.setenv("STOCK_AGENT_ENV", "test")
    store = StrategyValidationStore(tmp_path / "v.db")
    store.insert(run)
    assert store.latest("MEAN_REVERSION")["universe_integrity"]["status"] == "UNTRUSTED"
    rec["candidates"][0]["features"]["recent_return"] -= 1.0
    rec["audit"]["violations"] = MRA.audit(rec, ds, top_n=5, lookback=5)
    rec["audit"]["status"] = "FAIL" if rec["audit"]["violations"] else "PASS"
    assert IG.evaluate(rec).status == "BLOCKED"


def test_G02_registry_entry_is_an_implemented_research_strategy_that_never_trades():
    from strategy_registry import service as S
    from strategy_registry.standard import check_signal
    from test_product_flow_612 import signal
    e = R.get("MEAN_REVERSION")
    assert e.implemented and e.status == "RESEARCH" and e.version == MR.STRATEGY_VERSION and e.direction_capability == "LONG" and e.time_horizon == "SWING"
    assert e.source == "INTERNAL" and e.setup_type is None and not e.placeholder_note
    assert [s["strategy_id"] for s in S.registry_view({"available": False})["strategies"] if s["active"]] == ["DOWNSIDE_DISLOCATION_RECOVERY"]
    assert check_signal(signal(strategy_id="MEAN_REVERSION"), R.by_id())["error"]["code"] == "STRATEGY_INACTIVE"
    assert R.PLACEHOLDER_IDS == ()                                                                # Stage 6.7: BREAKOUT implemented
    assert not re.search(r"(?i)broker|account|quantity|order", json.dumps(e.model_dump()))


def test_G03_the_strategy_lab_detail_has_the_thirteen_sections_and_no_invented_result():
    from strategy_registry import service as S
    d = S.detail_view("MEAN_REVERSION", {"available": False}, validation={"validation_status": "NOT_RUN"})
    keys = [s["key"] for s in d["sections"]]
    assert keys == ["overview", "hypothesis", "rules", "data_requirements", "entry_logic", "invalidation", "exit_logic", "holding_horizon", "backtest",
                    "out_of_sample", "risk_notes", "paper_results", "decision_log"]
    st = {s["key"]: s for s in d["sections"]}
    assert d["links"] == {"design_docs": "DESIGN_66_MEAN_REVERSION.md"} and "SURVIVORSHIP_BIAS_PRESENT" in " ".join(st["risk_notes"]["items"])
    f = st["overview"]["facts"]
    assert (f["lookback_sessions"], f["top_n"], f["rebalance"], f["universe_method"]) == (5, 20, "WEEKLY", "FIXED_CURRENT_UNIVERSE") and f["selection_control"]
    assert "most negative" in " ".join(st["rules"]["items"])
    assert not re.search(r"\d+(\.\d+)?\s?%(?! each)", json.dumps(d["sections"]).replace("5 % each", "")) and "Sharpe" not in json.dumps(d["sections"])
    src = now_text("strategy_registry/mean_reversion.py")
    assert not re.search(r"requests|urllib|socket|sqlite3|broker|submit|order\(", src)


def test_G04_cli_validates_and_persists_mean_reversion(tmp_path, monkeypatch):
    from strategy_validation import cli
    from strategy_validation.dataset import dump
    monkeypatch.setenv("STOCK_AGENT_ENV", "test")
    p = tmp_path / "m.json"
    dump(base_ds(), str(p))
    out = io.StringIO()
    code = cli.main(["validate", "--strategy", "MEAN_REVERSION", "--dataset", str(p), "--db", str(tmp_path / "v.db"), "--persist", "--output", str(tmp_path / "r.json")], out=out)
    t = out.getvalue()
    assert code == 0 and t.startswith("MEAN_REVERSION v1.0:") and "selection edge (vs Equal-weight eligible universe):" in t and "universe: FIXED_CURRENT_UNIVERSE -> UNTRUSTED" in t
    assert "stored run" in t, t
    rec = json.loads((tmp_path / "r.json").read_text(encoding="utf-8"))
    assert rec["evaluation_mode"] == "CROSS_SECTIONAL_PORTFOLIO" and rec["config_snapshot"]["adapter"]["params"] == {} and "SURVIVORSHIP_BIAS_PRESENT" in rec["research_flags"]


def test_G05_a_replay_opens_no_socket_and_imports_no_broker(monkeypatch):
    def refuse(*a, **k):
        raise AssertionError("network access during a replay")
    monkeypatch.setattr(socket, "socket", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)
    assert E.run_validation(MeanReversionAdapter({"top_n": 5}), F.dataset(F.universe(25)), registry=R.by_id(), created_at=AT).candidate_count > 0
    for f in ("__init__.py", "adapter.py", "audit.py", "core.py", "data.py", "diagnostics.py"):
        tree = ast.parse(now_text(f"mean_reversion/{f}"))
        mods = {n.module or "" for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)} | {a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
        assert not any(m.startswith(("execution", "safety", "paper", "portfolio", "proposals", "api", "requests", "urllib", "httpx", "socket", "alpaca", "anthropic",
                                     "value_quality")) for m in mods), (f, mods)


def test_G08_frozen_controls_and_ci_manifest():
    import execution as X
    import forward_validation as FV
    assert FV.frozen_hash() == FROZEN_HASH and X.MODES == ("MONITOR_ONLY", "PAPER_AUTO")
    assert "test_mean_reversion_66.py" in now_text(".github/ci/pytest_groups.json")
    assert (MR.SPLIT_SESSION, MR.NORMAL_DAY, MR.SPLIT_DAY_TOL) == (0.01, 0.25, 0.02)                 # the Stage 6.5 split audit's classification
    from momentum import audit as MA
    assert (MA.SPLIT_SESSION, MA.NORMAL_DAY, MA.SPLIT_DAY_TOL) == (MR.SPLIT_SESSION, MR.NORMAL_DAY, MR.SPLIT_DAY_TOL)


def test_G06_the_rules_are_the_frozen_ones():
    r = subprocess.run(["git", "show", f"{FREEZE}:mean_reversion/__init__.py"], cwd=ROOT, capture_output=True, text=True, encoding="utf-8")
    if r.returncode != 0:
        pytest.skip("the freeze commit is not reachable (shallow clone)")
    frozen: dict = {}
    exec(compile(r.stdout, "frozen", "exec"), frozen)                                              # noqa: S102 - the repository's own file at a fixed commit
    for k in ("STRATEGY_VERSION", "RESEARCH_UNIVERSE", "MIN_PRICE", "MIN_MEDIAN_DOLLAR_VOLUME", "DOLLAR_VOLUME_SESSIONS", "LOOKBACK_SESSIONS", "SPLIT_SESSION",
              "NORMAL_DAY", "SPLIT_DAY_TOL", "ADJUSTED_LOSS_TOL", "TOP_N", "POSITION_PCT", "REBALANCE", "FRICTION_BPS_PER_SIDE", "SENSITIVITY",
              "CONCENTRATION_TOP_NAMES", "CONCENTRATION_NAME_SHARE"):
        assert frozen[k] == getattr(MR, k), k                                                      # nothing tuned after the OOS result
    for f in ("mean_reversion/core.py", "mean_reversion/data.py", "mean_reversion/adapter.py"):
        assert subprocess.run(["git", "diff", "--quiet", FREEZE, "--", f], cwd=ROOT).returncode == 0, f   # the rule code is byte-identical


def test_G07_documents_state_the_hypothesis_the_result_and_the_limits():
    d = now_text("DESIGN_66_MEAN_REVERSION.md")
    for s in ("## 1. Hypothesis (written before any code)", "## 2. Definition (v1, frozen before the OOS run)", "## 3. Feasibility", "## 5. Results",
              "SURVIVORSHIP_BIAS_PRESENT", "FIXED_CURRENT_UNIVERSE", "UNTRUSTED", "NOT_ESTABLISHED", "SELECTION CONTROL", "MARKET BENCHMARK",
              "NOT SELECTION-EDGE EVIDENCE", "## 6a. Development log", "## 7. Adversarial review", "## 8. Limitations", "Backtest results are hypothetical.",
              "626fb11", "DATA_INCONSISTENT", "never clipped"):
        assert s in d, s
    readme = now_text("README.md")
    sec = re.findall(r"(?ms)^## Mean Reversion strategy \(Stage 6\.6, research only\)\n.*?(?=^## )", readme)
    assert len(sec) == 1 and "python -m strategy_validation validate --strategy MEAN_REVERSION" in sec[0] and "SURVIVORSHIP_BIAS_PRESENT" in sec[0]
    assert readme.index("## Universe integrity (Stage 6.5.1)") < readme.index("## Mean Reversion strategy (Stage 6.6") < readme.index("## Continuous integration (Stage 6.0.3")
    assert not re.search(r"(?i)\b(guaranteed|profitable|buy now|sell now|regime)\b", sec[0])


def test_G09_the_real_mean_reversion_result_where_the_local_data_exists():
    db = ROOT / "runtime" / "preview_66.db"
    if not db.exists():
        pytest.skip("local data only: the Mean Reversion run lives in a git-ignored runtime database")
    from strategy_validation.store import StrategyValidationStore
    run = StrategyValidationStore(db).latest("MEAN_REVERSION")
    if run is None:
        pytest.skip("no stored Mean Reversion run in this local database")
    oo = run["cross_sectional"]["segments"]["OOS"]
    assert run["validation_version"] == "validation_v3" and run["eligibility"]["status"] == "ELIGIBLE" and run["cross_sectional"]["parity"]["status"] == "PASS"
    assert run["git_commit_sha"].startswith(FREEZE) and run["audit"]["status"] == "PASS"                          # the frozen code, a clean audit
    assert run["cross_sectional"]["selection_edge"]["status"] == run["selection_edge"]["status"] == "NOT_ESTABLISHED" and oo["period_count"] == 98
    assert oo["excess_t_stat"] < 1.0 and run["verdict"]["verdict"] == "FAIL" and run["performance_verdict"]["verdict"] == "PASS"
    assert run["universe_integrity"]["status"] == "UNTRUSTED" and "SURVIVORSHIP_BIAS_PRESENT" in run["research_flags"] and R.get("MEAN_REVERSION").status == "RESEARCH"
