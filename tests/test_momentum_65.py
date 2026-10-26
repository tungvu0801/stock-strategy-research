"""Stage 6.5 — Strategy #3: Momentum v1 (momentum/, strategy_registry/momentum.py, DESIGN_65_MOMENTUM.md).

  S  the signal: deterministic 12-1 return, the endpoints, the skipped recent month, no future data, splits, missing / short histories
  E  eligibility and ranking: price, liquidity, one listing per issuer, ties, top N
  L  the adapter: monthly legs, entry / exit timing, ENTER / HOLD / EXIT flags, candidate stability, one candidate set for every arm
  V  validation_v3: the control universe, control fairness, one observation per period, friction, turnover, sensitivity on TRAIN only
  U  the independent audit (it re-derives the selection; tampered records are caught; it never imports the core)
  G  the integrity gate, the registry, the Strategy Lab, the CLI, isolation, pre-registration, documents

Fixtures are SYNTHETIC (tests/momentum_fixtures.py). The real run is reproduced by `python -m strategy_validation validate --strategy MOMENTUM
--dataset runtime/vq/vq_bundle.json`; test_G09 checks it only where that local data exists."""
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
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

import momentum as M  # noqa: E402
import momentum_fixtures as F  # noqa: E402
from momentum import audit as MA  # noqa: E402
from momentum.adapter import MomentumAdapter  # noqa: E402
from momentum.core import INELIGIBLE, MomentumStrategyCore, pct_ranks  # noqa: E402
from momentum.data import PriceData  # noqa: E402
from strategy_registry import registry as R  # noqa: E402
from strategy_validation import engine as E  # noqa: E402
from strategy_validation import integrity as IG  # noqa: E402
from strategy_validation.dataset import Dataset  # noqa: E402

AT = "2026-10-26T00:00:00+00:00"
FROZEN_HASH = "b5d4a56ef5bba61f8250b3cd25182cc1655738adf0605a77671fd029883f8530"
FREEZE = "ad3e71c"


def now_text(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8").replace("\r\n", "\n")


@functools.lru_cache(maxsize=None)
def base_ds():
    return F.dataset(F.universe(25))


@functools.lru_cache(maxsize=None)
def full_run():
    ds = base_ds()
    return ds, E.run_validation(MomentumAdapter({"top_n": 5}), ds, registry=R.by_id(), created_at=AT)


def rec_of(run) -> dict:
    return json.loads(run.model_dump_json())


def evaluate(ds, T, top_n=5, **kw):
    data = PriceData(ds)
    syms = [s for s in M.RESEARCH_UNIVERSE if s in ds.bars]
    return MomentumStrategyCore(top_n=top_n, **kw).evaluate(T, syms, data)


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


def T_mid(ds):
    return MomentumStrategyCore().schedule(ds.sessions)[10]


# ================================================================ S the signal =====================================================================

def test_S01_the_momentum_is_the_12_1_adjusted_return_between_two_fixed_endpoints():
    ds = base_ds()
    T = T_mid(ds)
    i = ds.sessions.index(T)
    out = evaluate(ds, T)
    x = out["ranked"][0]
    inp = x["snapshot"]["inputs"]
    assert (inp["lookback_start_date"], inp["lookback_end_date"]) == (ds.sessions[i - 252], ds.sessions[i - 21])
    closes = {b.date: b.close for b in ds.bars[x["symbol"]]}
    assert x["momentum"] == pytest.approx(closes[ds.sessions[i - 21]] / closes[ds.sessions[i - 252]] - 1.0)
    assert (M.LOOKBACK_SESSIONS, M.SKIP_SESSIONS) == (252, 21) and evaluate(ds, T)["ranked"] == out["ranked"]   # deterministic


def test_S02_the_recent_month_never_enters_the_signal():
    ds = base_ds()
    T = T_mid(ds)
    i = ds.sessions.index(T)
    skipped = set(ds.sessions[i - 20:i + 1])                                                       # the 21 sessions after the end endpoint, T included
    loser = evaluate(ds, T)["ranked"][-1]["symbol"]
    moved = rebuild(ds, fn_bar=lambda s, d, v: v * (5.0 if (s == loser and d in skipped) else 1.0))   # a +400 % move inside the skipped month
    a, b = evaluate(ds, T), evaluate(moved, T)
    assert {x["symbol"]: x["momentum"] for x in a["ranked"]} == {x["symbol"]: x["momentum"] for x in b["ranked"]} and a["selected"] == b["selected"]
    i_end = ds.sessions[i - 21]
    moved_end = rebuild(ds, fn_bar=lambda s, d, v: v * (5.0 if (s == loser and d >= i_end) else 1.0))   # ... while the end endpoint itself counts (12-1, not 12-0)
    assert {x["symbol"]: x["momentum"] for x in evaluate(moved_end, T)["ranked"]}[loser] > {x["symbol"]: x["momentum"] for x in a["ranked"]}[loser] + 3


def test_S03_no_future_price_can_change_the_score_or_the_selection_at_T():
    ds = base_ds()
    T = T_mid(ds)
    future = rebuild(ds, fn_bar=lambda s, d, v: v * (0.1 if d > T else 1.0) * (7.0 if (d > T and s in ("AAPL", "ABBV")) else 1.0),
                     fn_raw=lambda s, d, v: v * (0.1 if d > T else 1.0))
    a, b = evaluate(ds, T), evaluate(future, T)
    assert [(x["symbol"], x["momentum"], x["percentile"], x["rank"]) for x in a["ranked"]] == [(x["symbol"], x["momentum"], x["percentile"], x["rank"]) for x in b["ranked"]]
    assert a["selected"] == b["selected"]
    ca = [c for c in MomentumAdapter({"top_n": 5}).candidates(ds) if c.signal_date == T]
    cb = [c for c in MomentumAdapter({"top_n": 5}).candidates(future) if c.signal_date == T]
    assert [(c.symbol, c.score, c.features["momentum_return"]) for c in ca] == [(c.symbol, c.score, c.features["momentum_return"]) for c in cb]


def test_S04_a_split_never_creates_momentum():
    plain = F.universe(25)
    split = copy.deepcopy(plain)
    split[3]["split"] = (F.weekdays("2019-01-02", "2022-12-30")[520], 4.0)                         # ACN splits 4-for-1 inside later lookbacks
    a, b = F.dataset(plain), F.dataset(split)
    for T in MomentumStrategyCore().schedule(a.sessions):
        ea, eb = evaluate(a, T), evaluate(b, T)
        assert [x["symbol"] for x in ea["ranked"]] == [x["symbol"] for x in eb["ranked"]] and ea["selected"] == eb["selected"], T   # the same ranking
        assert all(x["momentum"] == pytest.approx(y["momentum"], rel=1e-6, abs=1e-6) for x, y in zip(ea["ranked"], eb["ranked"]))   # (fixture rounding only)
    raw = b.aux["series"]["raw_close"]["ACN"]
    d0, d1 = split[3]["split"][0], F.weekdays("2019-01-02", "2022-12-30")[519]
    assert raw[d0] < raw[d1] / 3                                                                    # the raw series does show the split


def test_S05_a_short_history_is_excluded_never_shortened_padded_or_substituted():
    cos = F.universe(25)
    cos[5]["start"] = "2020-06-01"                                                                  # ADI listed later: < 252 sessions at early rebalances
    ds = F.dataset(cos)
    T = next(t for t in MomentumStrategyCore().schedule(ds.sessions) if t >= "2020-08-01")       # listed, but < 252 sessions of history
    out = evaluate(ds, T)
    assert "ADI" not in [x["symbol"] for x in out["ranked"]] and out["summary"]["ineligible_by_reason"]["INSUFFICIENT_HISTORY"] == 1
    gap = F.universe(25)
    T2 = MomentumStrategyCore().schedule(F.weekdays("2019-01-02", "2022-12-30"))[3]
    i2 = F.weekdays("2019-01-02", "2022-12-30").index(T2)
    gap[6]["gaps"] = [F.weekdays("2019-01-02", "2022-12-30")[i2 - 252]]                            # AMAT has no bar on the start endpoint
    out2 = evaluate(F.dataset(gap), T2)
    assert "AMAT" not in [x["symbol"] for x in out2["ranked"]] and out2["summary"]["ineligible_by_reason"]["MISSING_ENDPOINT_BAR"] == 1


# ================================================================ E eligibility + ranking ===========================================================

def test_E01_price_and_liquidity_filters_and_one_listing_per_issuer():
    cos = F.universe(25)
    cos[0]["price"] = 2.0                                                                           # AAPL below 5 USD (raw)
    cos[1]["volume"] = 1000.0                                                                       # ABBV illiquid
    cos[2]["cik"] = cos[3]["cik"]                                                                   # ABT and ACN: one issuer -> ACN is a second listing
    out = evaluate(F.dataset(cos), T_mid(base_ds()))
    r = out["summary"]["ineligible_by_reason"]
    assert r == {"PRICE_BELOW_MIN": 1, "LIQUIDITY_BELOW_MIN": 1, "SECOND_LISTING": 1} and set(r) <= set(INELIGIBLE)
    assert {"AAPL", "ABBV", "ACN"}.isdisjoint(x["symbol"] for x in out["ranked"]) and "ABT" in [x["symbol"] for x in out["ranked"]]


def test_E02_ties_rank_by_symbol_and_percentiles_are_tie_averaged():
    assert pct_ranks([("B", 0.1), ("A", 0.1), ("C", 0.3)]) == {"A": 0.5, "B": 0.5, "C": 1.0}
    cos = [F.stock(s, drift=0.0004, price=40.0, phase=0.0) for s in F.SYMBOLS[:6]]                 # identical paths: every momentum ties
    out = evaluate(F.dataset(cos), MomentumStrategyCore().schedule(F.weekdays("2019-01-02", "2022-12-30"))[2], top_n=3)
    assert out["selected"] == sorted(F.SYMBOLS[:6])[:3] and all(x["percentile"] == pytest.approx(3.5 / 6) for x in out["ranked"])


def test_E03_the_top_n_are_the_highest_momentum_stocks():
    out = evaluate(base_ds(), T_mid(base_ds()), top_n=5)
    moms = sorted((x["momentum"] for x in out["ranked"]), reverse=True)
    assert len(out["selected"]) == 5 and sorted(x["momentum"] for x in out["ranked"] if x["symbol"] in out["selected"]) == sorted(moms[:5])
    assert [x["rank"] for x in out["ranked"]] == list(range(1, len(out["ranked"]) + 1))
    assert M.TOP_N == 20 and MomentumStrategyCore().top_n == 20 and M.POSITION_PCT == 5.0


# ================================================================ L the adapter =====================================================================

def test_L01_monthly_legs_enter_at_the_next_open_and_exit_at_the_open_after_the_next_rebalance():
    ds, run = full_run()
    sched = MomentumStrategyCore().schedule(ds.sessions)
    trades = {t.candidate_id: t for t in run.arms["RULES"].trades}
    for c in run.candidates[:60]:
        t = trades.get(c.candidate_id)
        if t is None:
            continue
        k = sched.index(c.signal_date)
        assert t.entry_date == ds.sessions[ds.sessions.index(c.signal_date) + 1] and t.entry_raw == ds.bar(c.symbol, t.entry_date).open
        if k + 1 < len(sched):
            assert t.exit_date == ds.sessions[ds.sessions.index(sched[k + 1]) + 1] and t.exit_raw == ds.bar(c.symbol, t.exit_date).open and t.exit_reason == "TIME_EXIT"
    assert all(s[:7] != ds.sessions[ds.sessions.index(s) - 1][:7] and ds.sessions.index(s) >= 252 for s in sched)


def test_L02_transaction_flags_and_friction_only_on_real_orders():
    ds, run = full_run()
    slip = run.friction_model["slippage_bps_per_side"] / 10000.0
    legs = {c.candidate_id: c for c in run.candidates}
    for t in run.arms["RULES"].trades:
        c = legs[t.candidate_id]
        assert t.entry_fill == pytest.approx(t.entry_raw * (1 + slip) if c.entry_trade else t.entry_raw)
        assert t.exit_fill == pytest.approx(t.exit_raw * (1 - slip) if c.exit_trade else t.exit_raw)
    assert run.execution_assumptions["stop_policy"] == "NONE" and run.execution_assumptions["time_exit_fill"] == "OPEN"


def test_L03_candidates_are_stable_and_every_arm_gets_the_same_set():
    ds, run = full_run()
    again = E.run_validation(MomentumAdapter({"top_n": 5}), F.dataset(F.universe(25)), registry=R.by_id(), created_at=AT)
    assert run.candidate_set_hash == again.candidate_set_hash and run.cross_sectional["segments"] == again.cross_sectional["segments"]
    assert run.integrity["same_candidate_set"] and run.arms["GATED"].metrics == run.arms["RULES"].metrics and run.arms["MODEL"].status == "NOT_EVALUATED"


# ================================================================ V validation_v3 ===================================================================

def test_V01_the_control_is_the_same_eligible_universe_and_every_parity_check_passes():
    ds, run = full_run()
    cs = run.cross_sectional
    assert run.evaluation_mode == "CROSS_SECTIONAL_PORTFOLIO" and cs["control"]["control_id"] == "EQUAL_WEIGHT_ELIGIBLE_UNIVERSE"
    assert cs["control"]["role"] == "SELECTION_CONTROL" and cs["market_benchmark"]["role"] == "MARKET_BENCHMARK" and cs["market_benchmark"]["symbol"] == "SPY"
    assert cs["parity"]["status"] == "PASS" and len(cs["parity"]["checks"]) == 8
    snaps = MomentumAdapter({"top_n": 5}).selection_snapshots(ds)
    by = {c.signal_date: c.features["eligible_count"] for c in run.candidates}
    assert all(len(s["eligible"]) == by[s["rebalance"]] for s in snaps if s["rebalance"] in by)


def test_V02_one_observation_per_period_and_the_boundary_on_both_sides():
    ds, run = full_run()
    cs = run.cross_sectional
    inc = [p for p in cs["periods"] if p["status"] == "INCLUDED"]
    assert all(p["strategy"]["positions"] == 5 for p in inc) and cs["segments"]["ALL"]["period_count"] == len(inc)
    assert cs["excluded_periods"] == {"BOUNDARY": 1} and run.arms["RULES"].metrics["ALL"]["trade_count"] == 5 * len(inc)


def test_V03_turnover_and_friction_diagnostics():
    ds, run = full_run()
    d = run.diagnostics["strategy"]
    log = MomentumAdapter({"top_n": 5})
    list(log.candidates(ds))
    rl = log.rebalance_log[ds.dataset_id]
    assert d["construction"]["average_one_way_turnover"] == pytest.approx(sum(len(r["enter"]) for r in rl[1:]) / 5 / (len(rl) - 1), abs=1e-4)
    f = d["friction"]["ALL"]
    assert f["mean_excess_gross"] >= f["mean_excess_net"] and 0 <= (f["share_of_gross_excess_consumed"] or 0) < 1
    assert set(d["market_months"]["ALL"]) == {"benchmark_up", "benchmark_down"} and set(d["calendar"]) == {"by_year", "by_quarter"}


def test_V04_the_sensitivity_never_sees_a_bar_after_the_train_end():
    ds, run = full_run()
    s = run.diagnostics["strategy"]["sensitivity"]
    assert [r["params"] for r in s["rows"]] == ["base"] + list(M.SENSITIVITY) and "TRAIN view only" in s["scope"]
    wrecked = rebuild(ds, fn_bar=lambda sym, d, v: v * (3.0 if d > run.train_end else 1.0), fn_raw=lambda sym, d, v: v * (3.0 if d > run.train_end else 1.0))
    from momentum import diagnostics as MD
    assert MD.sensitivity(wrecked, run.train_end, 0.0005, {"top_n": 5, "lookback_sessions": 252, "skip_sessions": 21}) == \
        MD.sensitivity(ds, run.train_end, 0.0005, {"top_n": 5, "lookback_sessions": 252, "skip_sessions": 21})


def test_V05_survivorship_is_flagged_and_the_overall_verdict_needs_a_selection_edge():
    ds, run = full_run()
    assert run.research_flags == ["SURVIVORSHIP_BIAS_PRESENT"]
    from strategy_validation import cross_sectional as CS
    assert run.verdict["verdict"] == CS.overall_verdict(run.performance_verdict, run.selection_edge["status"])["verdict"]
    assert run.selection_edge["status"] in ("NOT_ESTABLISHED", "WEAK", "ESTABLISHED", "INSUFFICIENT_DATA") and R.get("MOMENTUM").status == "RESEARCH"


# ================================================================ U the independent audit ===========================================================

def test_U01_the_audit_re_derives_the_selection_and_passes_on_the_real_record():
    ds, run = full_run()
    assert MA.audit(rec_of(run), ds, top_n=5, lookback=252, skip=21) == [] and run.audit.status == "PASS"
    tree = ast.parse(now_text("momentum/audit.py"))
    mods = {n.module or "" for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)} | {a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
    assert not any(m.startswith(("momentum.core", "momentum.data", "momentum.adapter")) for m in mods), mods   # never the strategy's own code


@pytest.mark.parametrize("check,tamper", [
    ("MOMENTUM_RETURN", lambda c: c["features"].update(momentum_return=c["features"]["momentum_return"] + 0.5)),
    ("LOOKBACK_ENDPOINTS", lambda c: c["features"].update(lookback_end_date=c["signal_date"])),                       # a 12-0 endpoint
    ("NO_FUTURE_DATA", lambda c: c["feature_as_of"].update(momentum_return="2099-01-01")),
    ("ELIGIBILITY", lambda c: c["features"].update(eligible_count=c["features"]["eligible_count"] - 3)),
    ("RANK_SELECTION", lambda c: c["features"].update(rank=c["features"]["rank"] + 1)),
    ("RANK_SELECTION", lambda c: c.update(score=c["score"] - 10)),
    ("TRANSACTION_FLAGS", lambda c: c.update(entry_trade=not c["entry_trade"])),
    ("EXIT_AT_REBALANCE", lambda c: c.update(max_hold_bars=c["max_hold_bars"] + 2)),
])
def test_U02_a_tampered_record_is_caught(check, tamper):
    ds, run = full_run()
    rec = rec_of(run)
    victim = next(c for c in rec["candidates"] if c["signal_date"] == MomentumStrategyCore().schedule(ds.sessions)[6])
    tamper(victim)
    assert check in {v["check"] for v in MA.audit(rec, ds, top_n=5, lookback=252, skip=21) if v["candidate_id"] in (victim["candidate_id"], None)}


def test_U03_a_swapped_selection_and_a_fake_split_move_are_caught():
    ds, run = full_run()
    rec = rec_of(run)
    T = MomentumStrategyCore().schedule(ds.sessions)[6]
    legs = [c for c in rec["candidates"] if c["signal_date"] == T]
    out = evaluate(ds, T, top_n=5)
    outsider = next(x["symbol"] for x in out["ranked"] if x["symbol"] not in out["selected"])
    legs[0]["symbol"] = outsider                                                                     # a stock outside the top 5
    assert any(v["check"] == "RANK_SELECTION" and v["candidate_id"] is None for v in MA.audit(rec, ds, top_n=5, lookback=252, skip=21))
    cos = F.universe(25)
    d = F.weekdays("2019-01-02", "2022-12-30")
    cos[3]["split"] = (d[520], 4.0)
    sds = F.dataset(cos)
    srun = E.run_validation(MomentumAdapter({"top_n": 5}), sds, registry=R.by_id(), created_at=AT)
    assert srun.audit.status == "PASS"
    bad = rebuild(sds, fn_bar=lambda s, dd, v: v * (4.0 if (s == "ACN" and dd >= d[520]) else 1.0))   # an UN-adjusted split: a fake 4x move
    hit = [c for c in rec_of(srun)["candidates"] if c["symbol"] == "ACN" and c["features"]["lookback_start_date"] < d[520] <= c["features"]["lookback_end_date"]]
    assert hit                                                                                      # the split falls inside real lookbacks
    if hit:
        assert "SPLIT_CONSISTENCY" in {v["check"] for v in MA.audit({"candidates": hit}, bad, top_n=5, lookback=252, skip=21)}


def test_U04_every_split_of_every_stock_is_checked_across_holes_and_a_ratio_artifact_is_reported_not_failed():
    d = F.weekdays("2019-01-02", "2022-12-30")
    ds, run = full_run()
    loser = evaluate(ds, d[400], top_n=5)["ranked"][-1]["symbol"]                                   # a stock that is NOT selected
    cos = F.universe(25)
    k = next(i for i, c in enumerate(cos) if c["symbol"] == loser)
    cos[k]["split"] = (d[400], 4.0)
    cos[k]["gaps"] = [d[399]]                                                                        # and a hole the session before the split
    sds = F.dataset(cos)
    bad = rebuild(sds, fn_bar=lambda sym, dd, v: v * (4.0 if (sym == loser and dd >= d[400]) else 1.0))   # the split left UN-adjusted
    hits = [x for x in MA.audit({"candidates": []}, bad, top_n=5, lookback=252, skip=21) if x["check"] == "SPLIT_CONSISTENCY"]
    assert hits and loser in hits[0]["detail"] and "not absorbed" in hits[0]["detail"]
    assert [x for x in MA.audit({"candidates": []}, sds, top_n=5, lookback=252, skip=21) if x["check"] == "SPLIT_CONSISTENCY"] == []   # adjusted: fine
    art = copy.deepcopy(base_ds().aux)
    for dd in art["series"]["split_ratio"]["ADBE"]:
        if dd < d[500]:
            art["series"]["split_ratio"]["ADBE"][dd] = 0.5                                            # the provider's ratio moves, the raw price does not
    ads = Dataset({sym: [b.model_dump() for b in rows] for sym, rows in base_ds().bars.items()}, kind="SYNTHETIC", provenance=F.PROVENANCE, aux=art)
    evs = MA.split_events(ads, ["ADBE"])
    assert [e["kind"] for e in evs] == ["RATIO_ARTIFACT"]
    assert [x for x in MA.audit({"candidates": []}, ads, top_n=5, lookback=252, skip=21) if x["check"] == "SPLIT_CONSISTENCY"] == []   # reported, not failed
    from momentum import diagnostics as MD
    dq = MD.diagnostics(rec_of(full_run()[1]), ads, MomentumAdapter({"top_n": 5}).rebalance_log.get(ads.dataset_id) or [], top_n=5)["data_quality"]
    assert [a["symbol"] for a in dq["ratio_artifacts"]] == ["ADBE"] and abs(dq["ratio_artifacts"][0]["adjusted_vs_raw_step"]) < 1e-3


def test_V06_sensitivity_rows_share_the_same_periods_and_concentration_has_attribution():
    ds, run = full_run()
    d = run.diagnostics["strategy"]
    rows = d["sensitivity"]["rows"]
    assert len({r["periods"] for r in rows}) == 1 and all(r["own_periods"] >= r["periods"] for r in rows)
    c = d["concentration"]
    assert c["sic_major_group"]["most_frequent_top_groups"] and c["excess_contribution"]["top"]
    total = sum(v for _, v in c["excess_contribution"]["top"] + c["excess_contribution"]["bottom"])
    allsum = sum(v for v in [x[1] for x in c["excess_contribution"]["top"]])
    assert c["excess_contribution"]["summed_period_excess"] == pytest.approx(sum(p["excess"] for p in run.cross_sectional["periods"] if p["status"] == "INCLUDED"), abs=1e-6)
    assert isinstance(total, float) and isinstance(allsum, float)


# ================================================================ G gate, registry, Lab, CLI, isolation, documents =====================================

def test_G01_the_run_is_eligible_and_a_tampered_record_is_blocked():
    ds, run = full_run()
    rec = rec_of(run)
    assert IG.evaluate(rec).status == "ELIGIBLE" and {c.check: c.status for c in IG.evaluate(rec).checks}["SELECTION_CONTROL"] == "PASS"
    rec["candidates"][0]["features"]["momentum_return"] += 1.0
    rec["audit"]["violations"] = MA.audit(rec, ds, top_n=5, lookback=252, skip=21)
    rec["audit"]["status"] = "FAIL" if rec["audit"]["violations"] else "PASS"
    assert IG.evaluate(rec).status == "BLOCKED"


def test_G02_registry_entry_is_an_implemented_research_strategy_that_never_trades():
    from strategy_registry import service as S
    from strategy_registry.standard import check_signal
    from test_product_flow_612 import signal
    e = R.get("MOMENTUM")
    assert e.implemented and e.status == "RESEARCH" and e.version == M.STRATEGY_VERSION and e.direction_capability == "LONG" and e.time_horizon == "POSITION"
    assert e.source == "INTERNAL" and e.setup_type is None and not e.placeholder_note
    assert [s["strategy_id"] for s in S.registry_view({"available": False})["strategies"] if s["active"]] == ["DOWNSIDE_DISLOCATION_RECOVERY"]
    assert check_signal(signal(strategy_id="MOMENTUM"), R.by_id())["error"]["code"] == "STRATEGY_INACTIVE"
    assert R.PLACEHOLDER_IDS == ("MEAN_REVERSION", "BREAKOUT")


def test_G03_the_strategy_lab_detail_has_the_thirteen_sections_and_no_invented_result():
    from strategy_registry import service as S
    d = S.detail_view("MOMENTUM", {"available": False}, validation={"validation_status": "NOT_RUN"})
    keys = [s["key"] for s in d["sections"]]
    assert keys == ["overview", "hypothesis", "rules", "data_requirements", "entry_logic", "invalidation", "exit_logic", "holding_horizon", "backtest",
                    "out_of_sample", "risk_notes", "paper_results", "decision_log"]
    st = {s["key"]: s for s in d["sections"]}
    assert d["links"] == {"design_docs": "DESIGN_65_MOMENTUM.md"} and "SURVIVORSHIP_BIAS_PRESENT" in " ".join(st["risk_notes"]["items"])
    assert st["overview"]["facts"]["lookback_sessions"] == 252 and st["overview"]["facts"]["skip_sessions"] == 21 and st["overview"]["facts"]["selection_control"]
    assert not re.search(r"\d+(\.\d+)?\s?%(?! each)", json.dumps(d["sections"]).replace("5 % each", "")) and "Sharpe" not in json.dumps(d["sections"])
    src = now_text("strategy_registry/momentum.py")
    assert not re.search(r"requests|urllib|socket|sqlite3|broker|submit|order\(", src)


def test_G04_cli_validates_and_persists_momentum(tmp_path, monkeypatch):
    from strategy_validation import cli
    from strategy_validation.dataset import dump
    monkeypatch.setenv("STOCK_AGENT_ENV", "test")
    p = tmp_path / "m.json"
    dump(base_ds(), str(p))
    out = io.StringIO()
    code = cli.main(["validate", "--strategy", "MOMENTUM", "--dataset", str(p), "--db", str(tmp_path / "v.db"), "--persist", "--output", str(tmp_path / "r.json")], out=out)
    t = out.getvalue()
    assert code == 0 and t.startswith("MOMENTUM v1.0:") and "selection edge (vs Equal-weight eligible universe):" in t and "stored run" in t, t
    rec = json.loads((tmp_path / "r.json").read_text(encoding="utf-8"))
    assert rec["evaluation_mode"] == "CROSS_SECTIONAL_PORTFOLIO" and rec["config_snapshot"]["adapter"]["params"] == {} and rec["research_flags"] == ["SURVIVORSHIP_BIAS_PRESENT"]


def test_G05_a_replay_opens_no_socket_and_imports_no_broker(monkeypatch):
    def refuse(*a, **k):
        raise AssertionError("network access during a replay")
    monkeypatch.setattr(socket, "socket", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)
    assert E.run_validation(MomentumAdapter({"top_n": 5}), F.dataset(F.universe(25)), registry=R.by_id(), created_at=AT).candidate_count > 0
    for f in ("__init__.py", "adapter.py", "audit.py", "core.py", "data.py", "diagnostics.py"):
        tree = ast.parse(now_text(f"momentum/{f}"))
        mods = {n.module or "" for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)} | {a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
        assert not any(m.startswith(("execution", "safety", "paper", "portfolio", "proposals", "api", "requests", "urllib", "httpx", "socket", "alpaca", "anthropic",
                                     "value_quality")) for m in mods), (f, mods)


def test_G06_the_rules_are_the_frozen_ones_and_the_universe_is_value_qualitys():
    import value_quality as VQ
    assert M.RESEARCH_UNIVERSE == VQ.RESEARCH_UNIVERSE and len(M.RESEARCH_UNIVERSE) == 81
    r = subprocess.run(["git", "show", f"{FREEZE}:momentum/__init__.py"], cwd=ROOT, capture_output=True, text=True)
    if r.returncode != 0:
        pytest.skip("the freeze commit is not reachable (shallow clone)")
    frozen: dict = {}
    exec(compile(r.stdout, "frozen", "exec"), frozen)                                              # noqa: S102 - the repository's own file at a fixed commit
    for k in ("STRATEGY_VERSION", "RESEARCH_UNIVERSE", "MIN_PRICE", "MIN_MEDIAN_DOLLAR_VOLUME", "DOLLAR_VOLUME_SESSIONS", "LOOKBACK_SESSIONS", "SKIP_SESSIONS",
              "TOP_N", "POSITION_PCT", "REBALANCE", "FRICTION_BPS_PER_SIDE", "SENSITIVITY"):
        assert frozen[k] == getattr(M, k), k                                                       # nothing tuned after the OOS result
    for f in ("momentum/core.py", "momentum/data.py"):
        assert subprocess.run(["git", "diff", "--quiet", FREEZE, "--", f], cwd=ROOT).returncode == 0, f   # the rule code is byte-identical
    frozen_adapter = subprocess.run(["git", "show", f"{FREEZE}:momentum/adapter.py"], cwd=ROOT, capture_output=True, text=True, encoding="utf-8").stdout.replace("\r\n", "\n")
    decl = re.findall(r"(?ms)^    # Stage 6\.5\.1: the universe declaration.*?\n(?=\n    def __init__)", now_text("momentum/adapter.py"))
    assert len(decl) == 1 and now_text("momentum/adapter.py").replace(decl[0], "") == frozen_adapter   # + only the Stage 6.5.1 universe declaration


def test_G07_documents_state_the_hypothesis_the_result_and_the_limits():
    d = now_text("DESIGN_65_MOMENTUM.md")
    for s in ("## 1. Hypothesis (written before any code)", "## 2. Definition (v1, frozen before the OOS run)", "12-1", "SURVIVORSHIP_BIAS_PRESENT",
              "## 3. Feasibility", "## 5. Results", "NOT_ESTABLISHED", "SELECTION CONTROL", "MARKET BENCHMARK", "## 7. Adversarial review", "## 8. Limitations",
              "Backtest results are hypothetical.", "freeze"):
        assert s in d, s
    readme = now_text("README.md")
    sec = re.findall(r"(?ms)^## Momentum strategy \(Stage 6\.5, research only\)\n.*?(?=^## )", readme)
    assert len(sec) == 1 and "python -m strategy_validation validate --strategy MOMENTUM" in sec[0] and "SURVIVORSHIP_BIAS_PRESENT" in sec[0]
    assert readme.index("## Cross-sectional validation (Stage 6.4.1") < readme.index("## Momentum strategy (Stage 6.5") < readme.index("## Continuous integration (Stage 6.0.3")
    assert not re.search(r"(?i)\b(guaranteed|profitable|buy now|sell now|regime)\b", sec[0])


def test_G08_frozen_controls_and_ci_manifest():
    import execution as X
    import forward_validation as FV
    assert FV.frozen_hash() == FROZEN_HASH and X.MODES == ("MONITOR_ONLY", "PAPER_AUTO")
    assert "test_momentum_65.py" in now_text(".github/ci/pytest_groups.json")


def test_G09_the_real_momentum_result_where_the_local_data_exists():
    db = ROOT / "runtime" / "preview_65.db"
    if not db.exists():
        pytest.skip("local data only: the Momentum run lives in a git-ignored runtime database")
    from strategy_validation.store import StrategyValidationStore
    run = StrategyValidationStore(db).latest("MOMENTUM")
    if run is None:
        pytest.skip("no stored Momentum run in this local database")
    oo = run["cross_sectional"]["segments"]["OOS"]
    assert run["validation_version"] == "validation_v3" and run["eligibility"]["status"] == "ELIGIBLE" and run["cross_sectional"]["parity"]["status"] == "PASS"
    assert run["selection_edge"]["status"] == "NOT_ESTABLISHED" and oo["period_count"] == 22 and oo["excess_t_stat"] < 1.0 and run["verdict"]["verdict"] == "FAIL"
    assert run["performance_verdict"]["verdict"] == "PASS" and run["research_flags"] == ["SURVIVORSHIP_BIAS_PRESENT"] and R.get("MOMENTUM").status == "RESEARCH"
