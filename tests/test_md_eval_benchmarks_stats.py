"""
MD-B (md_eval): benchmarks and statistics.
  B01-B05  exposure-matched SPY (E = 1 -> SPY, E = 0 -> cash, the clamp, E(t-1) not E(t): control, rebalancing cost); B4 equal weight through
           the engine; the random-entry control: reproducible, seeds recorded, report only, V1 unaffected
  M01-M03  metrics on a hand-computed curve; Newey-West alpha / beta against an independent slope-variance formula; the lag rule
  S01-S04  the stationary bootstrap (seed reproducible, sensible p-values); PSR / DSR against an independent formula, N = 1, the fallback
           label; segment bounds refuse overlaps
  V01      the V1 bridge emits exactly the frozen core's signals (rules reused unchanged; hash 6c20fdb4...)
"""
import math
import random
from statistics import NormalDist

import pytest

import md_eval_fixtures as FX
import swing_pullback as SP
from md_eval import benchmarks as BM
from md_eval import engine as EN
from md_eval import inference as IN
from md_eval import metrics as MT
from md_eval.signals import EqualWeight, SwingBridge
from strategy_validation.dataset import Dataset
from strategy_validation.models import Bar
from swing_pullback.core import SwingPullbackCore
import research_gate as RGATE


def _fake(gross, equity, cash_r=None):
    cash_r = cash_r or [0.0] * len(equity)
    return {"rows": [{"gross_long": g, "equity": e, "cash_return": c, "exposure": g / e} for g, e, c in zip(gross, equity, cash_r)]}


# ============================================================================================================ benchmarks (B)
def test_B01_exposure_matched_spy_limits_and_the_clamp():
    spy = [0.0, 0.01, -0.02, 0.03]
    full = BM.exposure_matched(_fake([100.0] * 4, [100.0] * 4), spy)
    assert full["returns"] == spy and full["exposure"] == [1.0] * 4
    cash = [0.0, 0.001, 0.001, 0.001]
    none = BM.exposure_matched(_fake([0.0] * 4, [100.0] * 4, cash), spy)
    assert none["returns"] == [0.0] + cash[1:]
    clamp = BM.exposure_matched(_fake([150.0, -5.0, 50.0, 50.0], [100.0] * 4), spy)
    assert clamp["exposure"] == [1.0, 0.0, 0.5, 0.5]


def test_B02_the_exposure_of_t_minus_1_is_used_and_a_same_session_exposure_is_detected():
    spy = [0.0, 0.01, 0.02, -0.01, 0.005]
    a = BM.exposure_matched(_fake([0.0, 50.0, 50.0, 100.0, 0.0], [100.0] * 5), spy)
    b = BM.exposure_matched(_fake([0.0, 50.0, 50.0, 0.0, 0.0], [100.0] * 5), spy)     # only E(3) differs
    assert a["returns"][:4] == b["returns"][:4] and a["returns"][4] != b["returns"][4]
    assert a["returns"][1] == 0.0 and a["returns"][2] == 0.5 * 0.02 and a["returns"][4] == 1.0 * 0.005
    wrong = [0.0] + [min(1.0, r["gross_long"] / r["equity"]) * s for r, s in zip(_fake([0.0, 50.0, 50.0, 100.0, 0.0], [100.0] * 5)["rows"][1:], spy[1:])]
    assert wrong != a["returns"]                                                    # control: E(t) gives a different (look-ahead) series


def test_B03_rebalancing_cost_is_charged_on_the_trade_to_the_target_exposure():
    spy = [0.0, 0.01, 0.01, 0.01]
    r = BM.exposure_matched(_fake([100.0] * 4, [100.0] * 4), spy, friction_rate=0.001)
    assert math.isclose(r["returns_with_rebalancing_cost"][1], 0.01 - 0.001) and r["returns_with_rebalancing_cost"][2:] == spy[2:]
    assert BM.exposure_matched(_fake([100.0] * 4, [100.0] * 4), [0.0, None, 0.01, 0.01])["unavailable_sessions"] == 1


def test_B04_equal_weight_b4_runs_through_the_same_engine():
    n = 8
    d = FX.simple_double({"SIM:A": FX.flat(n), "SIM:B": FX.flat(n, 50.0)}, n=n)
    r = EN.run(d, EqualWeight(FX.UNIVERSE), FX.v1_config(d, cost_model=FX.zero_costs(), initial_cash=10_000.0))
    assert sorted((f["instrument_id"], f["shares"]) for f in r["fills"]) == [("SIM:A", 50), ("SIM:B", 100)]
    assert r["identity"]["book"] == "BENCHMARK_WEIGHTS" and r["final_equity"] == 10_000.0


def test_B05_the_random_entry_control_is_reproducible_records_its_seeds_and_never_touches_v1():
    d = FX.v1_double()
    cfg = FX.v1_config(d)
    v1_before = EN.run(d, SwingBridge(True, FX.UNIVERSE), cfg)
    adm = RGATE.admit_double(d)                                                             # I-1B-1 (O-7): an admitted dataset; admission is deterministic
    one = BM.random_control(adm, cfg, FX.UNIVERSE, [11, 12])
    two = BM.random_control(adm, cfg, FX.UNIVERSE, [11, 12])
    assert one == two and one["seeds"] == [11, 12] and one["role"] == "REPORT_ONLY_NEVER_TUNES_V1"
    assert one["runs"][0]["identity_sha256"] != one["runs"][1]["identity_sha256"]                    # the seed is in the identity
    v1_after = EN.run(d, SwingBridge(True, FX.UNIVERSE), cfg)
    assert v1_before["identity_sha256"] == v1_after["identity_sha256"] and v1_before["decisions"] == v1_after["decisions"]
    assert BM.percentile_of(1.0, [0.0, 2.0]) == 0.5 and BM.percentile_of(1.0, []) is None


# ============================================================================================================ metrics (M)
def test_M01_metrics_on_a_hand_computed_curve():
    rows = [{"session": f"s{i}", "equity": e, "gross_long": g, "exposure": g / e, "cash_return": 0.0} for i, (e, g) in
            enumerate([(100.0, 0.0), (110.0, 110.0), (99.0, 99.0), (121.0, 0.0)])]
    sm = MT.summary({"rows": rows, "initial_cash": 100.0, "traded_notional": 220.0, "total_costs": 0.0, "flags": []})
    assert math.isclose(sm["total_return"], 0.21) and math.isclose(sm["cagr"], 1.21 ** (252 / 3) - 1)
    assert math.isclose(sm["max_drawdown"], 99.0 / 110.0 - 1) and sm["max_drawdown_sessions"] == 1
    assert sm["time_in_market"] == 0.5 and math.isclose(sm["turnover_annualised"], 220.0 / 107.5 * 252 / 3)
    rets = [0.0, 0.1, 99 / 110 - 1, 121 / 99 - 1]
    m = sum(rets) / 4
    sd = math.sqrt(sum((x - m) ** 2 for x in rets) / 3)
    assert math.isclose(sm["sharpe_excess"], m / sd * math.sqrt(252))


def _ab_data(n=400, seed=3):
    rng = random.Random(seed)
    x = [rng.gauss(0, 0.01) for _ in range(n)]
    e = [0.0]
    for _ in range(n - 1):
        e.append(0.5 * e[-1] + rng.gauss(0, 0.004) * (1 + abs(x[len(e)]) * 50))
    return x, [0.0002 + 1.3 * xi + ei for xi, ei in zip(x, e)]


@pytest.mark.parametrize("lag", [0, 3, None])
def test_M02_newey_west_matches_an_independent_slope_variance(lag):
    x, y = _ab_data()
    r = MT.alpha_beta(y, x, lag=lag)
    n, L = len(x), (MT.newey_west_lag(len(x)) if lag is None else lag)
    mx, my = sum(x) / n, sum(y) / n
    sxx = sum((v - mx) ** 2 for v in x)
    b = sum((a - mx) * (c - my) for a, c in zip(x, y)) / sxx
    a0 = my - b * mx
    u = [(xi - mx) * (yi - a0 - b * xi) for xi, yi in zip(x, y)]
    var = sum(v * v for v in u) + 2 * sum((1 - l / (L + 1)) * sum(u[t] * u[t - l] for t in range(l, n)) for l in range(1, L + 1))
    assert math.isclose(r["beta"], b, rel_tol=1e-12) and math.isclose(r["alpha_daily"], a0, rel_tol=1e-9, abs_tol=1e-15)
    assert math.isclose(r["se_beta"], math.sqrt(var) / sxx, rel_tol=1e-9) and r["lag"] == L
    assert abs(r["beta"] - 1.3) < 0.1


def test_M03_exact_line_and_the_lag_rule():
    x = [0.01 * math.sin(k) for k in range(50)]
    r = MT.alpha_beta([0.001 + 1.5 * v for v in x], x, lag=0)
    assert math.isclose(r["alpha_daily"], 0.001, rel_tol=1e-9) and math.isclose(r["beta"], 1.5, rel_tol=1e-9) and r["se_beta"] < 1e-12
    assert MT.newey_west_lag(1000) == int(math.floor(4 * 10 ** (2 / 9))) == 6 and MT.newey_west_lag(100) == 4
    with pytest.raises(ValueError):
        MT.alpha_beta([1.0, 2.0, 3.0], [1.0, 1.0, 1.0])


# ============================================================================================================ inference (S)
def test_S01_the_stationary_bootstrap_is_reproducible_from_its_seed():
    rng = random.Random(5)
    x = [0.001 + rng.gauss(0, 0.01) for _ in range(300)]
    a, b = IN.stationary_bootstrap(x, 20, 300, seed=7), IN.stationary_bootstrap(x, 20, 300, seed=7)
    c = IN.stationary_bootstrap(x, 20, 300, seed=8)
    assert a == b and a["seed"] == 7 and a["percentiles"] != c["percentiles"] and a["method"] == "STATIONARY_BOOTSTRAP_BASIC"
    assert a["percentiles"]["5.0"] <= a["percentiles"]["50.0"] <= a["percentiles"]["95.0"]
    with pytest.raises(ValueError):
        IN.stationary_bootstrap([1.0], 20, 10, seed=1)


def test_S02_bootstrap_p_values_are_small_for_a_clear_edge_and_moderate_for_none():
    rng = random.Random(9)
    clear = [0.01 + rng.gauss(0, 0.005) for _ in range(250)]
    zero = [rng.gauss(0, 0.01) for _ in range(250)]
    zero = [v - sum(zero) / len(zero) + 1e-6 for v in zero]
    assert IN.stationary_bootstrap(clear, 10, 400, seed=1)["p_value_one_sided"] < 0.01
    assert 0.2 < IN.stationary_bootstrap(zero, 10, 400, seed=1)["p_value_one_sided"] < 0.8


def test_S03_psr_and_dsr_match_an_independent_formula():
    rng = random.Random(21)
    x = [0.0008 + rng.gauss(0, 0.01) for _ in range(500)]
    n = len(x)
    m = sum(x) / n
    sd = math.sqrt(sum((v - m) ** 2 for v in x) / (n - 1))
    m2 = sum((v - m) ** 2 for v in x) / n
    g3, g4 = (sum((v - m) ** 3 for v in x) / n) / m2 ** 1.5, (sum((v - m) ** 4 for v in x) / n) / m2 ** 2
    sr = m / sd
    phi = NormalDist()
    psr0 = phi.cdf(sr * math.sqrt(n - 1) / math.sqrt(1 - g3 * sr + (g4 - 1) / 4 * sr * sr))
    assert math.isclose(IN.psr(x)["psr"], psr0, rel_tol=1e-12)
    assert math.isclose(IN.psr(x, sr)["psr"], 0.5, rel_tol=1e-12)
    one = IN.dsr(x, 1)
    assert math.isclose(one["dsr"], psr0, rel_tol=1e-12) and one["labels"] == ["DSR_VARIANCE_FALLBACK_ESTIMATOR"]
    ten = IN.dsr(x, 10, trial_sharpes=[0.01, 0.03, -0.02, 0.05])
    tm = sum([0.01, 0.03, -0.02, 0.05]) / 4
    v = sum((s - tm) ** 2 for s in [0.01, 0.03, -0.02, 0.05]) / 3
    g = 0.5772156649015329
    sr0 = math.sqrt(v) * ((1 - g) * phi.inv_cdf(1 - 1 / 10) + g * phi.inv_cdf(1 - 1 / (10 * math.e)))
    assert math.isclose(ten["sr_star"], sr0, rel_tol=1e-12) and ten["dsr"] < psr0 and ten["labels"] == [] and ten["n_trials"] == 10
    with pytest.raises(ValueError):
        IN.dsr(x, 0)


def test_S04_segments_are_dated_and_never_overlap():
    ss = [f"2024-01-{d:02d}" for d in range(1, 31)]
    seg = IN.segment_bounds(ss, {"TRAIN": ["2024-01-01", "2024-01-10"], "VALIDATION": ["2024-01-11", "2024-01-20"], "TEST": ["2024-01-21", "2024-01-30"]})
    assert seg["TRAIN"] == ("2024-01-01", "2024-01-10", 10) and seg["TEST"][2] == 10
    with pytest.raises(ValueError):
        IN.segment_bounds(ss, {"TRAIN": ["2024-01-01", "2024-01-15"], "VALIDATION": ["2024-01-11", "2024-01-20"]})


# ============================================================================================================ V1 reuse (V)
def test_V01_the_bridge_emits_exactly_the_frozen_cores_signals():
    assert SP.preregistration_hash() == "6c20fdb4f9c093fed0a82c21c642dfceee2cc6d424104390c7c432e3cfa61903"
    d = FX.v1_double()
    s = d.sessions("0", "9")
    for rule in (True, False):
        src = SwingBridge(rule, FX.UNIVERSE)
        r = EN.run(d, src, FX.v1_config(d))
        assert {"EXITS_NEXT_OPEN", "SIGNAL_BASIS_SPLIT_ADJUSTED", "FIXED_CURRENT_UNIVERSE", "SURVIVORSHIP_BIAS_PRESENT"} <= set(r["flags"])
        bars, rc, rv = {}, {}, {}
        sym = {FX.AAPL: "AAPL", FX.MSFT: "MSFT", FX.SPY: "SPY"}
        for iid, name in sym.items():
            rows = d.bars(iid, s[0], s[-1])
            bars[name] = [Bar(date=b.session, open=b.raw_open / b.split_factor, high=b.raw_high / b.split_factor, low=b.raw_low / b.split_factor,
                              close=b.raw_close / b.split_factor, volume=b.raw_volume) for b in rows]
            rc[name] = {b.session: b.raw_close for b in rows}
            rv[name] = {b.session: b.raw_volume for b in rows}
        full = Dataset(bars, kind="SYNTHETIC", provenance={"volume_feed": "SIP"},
                       aux={"series": {"raw_close": rc, "raw_volume": rv}, "static": {"issuers": {"A": {"symbols": ["AAPL"]}, "M": {"symbols": ["MSFT"]}}}})
        want = sorted((sg["symbol"], sg["signal_date"], sg["stop"], sg["target"]) for sg in SwingPullbackCore(rule).evaluate(full)["signals"]
                      if sg["signal_date"] < s[-1])
        got = sorted((sym[x.instrument_id], dd, x.stop_raw, x.target_raw) for dd, x in src.emitted)
        assert want and got == want, (rule, len(want), len(got))
        assert any(x["action"] == "ENTRY" for x in r["decisions"]) and all(x.max_hold == SP.MAX_HOLD_BARS for _, x in src.emitted)
