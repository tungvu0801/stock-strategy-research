"""Stage 6.4 — Strategy #2: Value + Quality (value_quality/, strategy_registry/value_quality.py, DESIGN_64_VALUE_QUALITY.md).

  A  the rules: cheap AND profitable ranks high; cheap-but-weak and strong-but-expensive do not automatically win; negative earnings are not cheap;
     missing fundamentals are excluded and counted; percentile ranks; deterministic selection; the universe listing rules
  P  point in time: a filing is usable from the session after it (the Aug 10 filing test), a restatement is invisible until filed, TTM rules
  C  corporate actions and XBRL concepts: split re-basing of share counts, scale errors rejected, concept priority recorded
  L  the adapter: monthly rebalance membership, legs, ENTER / HOLD / EXIT flags, exits at the right rebalance, factor dates <= signal
  H  the harness extensions: stop-less equal-weight legs, OPEN time exits, friction only on real orders, aux data cut at the TRAIN end
  U  the independent strategy audit (tampered records are caught; it never imports the core)
  G  the integrity gate (research registry check, CANDIDATE_CONTRACT) and persistence
  S  the registry entry, the Strategy Lab sections, the survivorship disclosure in the validation payload (+ node tests), no lifecycle promotion
  X  isolation: no network in a replay, no broker / execution import, the CLI
  Z  pre-registration, documents, frozen controls

Fixtures are SYNTHETIC (tests/value_quality_fixtures.py) - the real run is reproduced by `python -m strategy_validation validate --strategy VALUE_QUALITY
--dataset runtime/vq/vq_bundle.json`, never by a test."""
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

import value_quality as VQ  # noqa: E402
import value_quality_fixtures as F  # noqa: E402
from strategy_registry import registry as R  # noqa: E402
from strategy_validation import engine as E  # noqa: E402
from strategy_validation.dataset import Dataset, dump  # noqa: E402
from value_quality import audit as VA  # noqa: E402
from value_quality import pit  # noqa: E402
from value_quality.adapter import ValueQualityAdapter, rebalance_sessions  # noqa: E402
from value_quality.core import INELIGIBLE, ValueQualityStrategyCore, pct_ranks  # noqa: E402
from value_quality.data import BundleData  # noqa: E402

AT = "2026-10-25T00:00:00+00:00"
FROZEN_HASH = "b5d4a56ef5bba61f8250b3cd25182cc1655738adf0605a77671fd029883f8530"             # the Stage 6.1 frozen controls: unchanged
T_JUN = "2021-06-01"


def now_text(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8").replace("\r\n", "\n")


def evaluate(ds, T, top_n=3, symbols=None, prev=None):
    data = BundleData(ds)
    syms = symbols or [s for s in VQ.RESEARCH_UNIVERSE if s in ds.bars]
    return ValueQualityStrategyCore(top_n=top_n).evaluate(T, syms, data, data.calendar, prev=prev)


def with_facts(ds, fn):
    """A new dataset whose XBRL facts were changed by fn(facts) (a deep copy: the original is untouched)."""
    aux = copy.deepcopy(ds.aux)
    fn(aux["events"]["facts"])
    return Dataset(ds.bars, kind=ds.kind, source=ds.source, provenance=ds.declared, aux=aux)


def cik(ds, symbol):
    return next(c for c, info in ds.aux["static"]["issuers"].items() if symbol in info["symbols"])


@functools.lru_cache(maxsize=None)
def ranking_ds():
    return F.dataset(F.ranking_set())


@functools.lru_cache(maxsize=None)
def full_run():
    ds = F.dataset(F.run_set())
    ad = ValueQualityAdapter({"top_n": 5})
    return ds, ad, E.run_validation(ad, ds, registry=R.by_id(), created_at=AT)


# ================================================================ A the rules ======================================================================

def test_A01_cheap_and_profitable_ranks_first():
    out = evaluate(ranking_ds(), T_JUN)
    by = {x["symbol"]: x for x in out["ranked"]}
    assert out["ranked"][0]["symbol"] == "AAPL" and by["AAPL"]["rank"] == 1 and out["selected"][0] == "AAPL"
    assert by["AAPL"]["value_score"] == by["AAPL"]["quality_score"] == 7 / 8                       # second on every factor of both groups
    assert by["AAPL"]["composite"] == pytest.approx(0.5 * 7 / 8 + 0.5 * 7 / 8)


def test_A02_cheap_but_low_quality_does_not_automatically_win():
    by = {x["symbol"]: x for x in evaluate(ranking_ds(), T_JUN)["ranked"]}
    junk = by["ABBV"]
    assert junk["value_score"] == 1.0 and junk["quality_score"] == 1 / 8                          # the cheapest on every value factor, the weakest on quality
    assert junk["rank"] > 1 and junk["composite"] < by["AAPL"]["composite"] and junk["rank"] > by["ACN"]["rank"]


def test_A03_high_quality_but_extremely_expensive_does_not_automatically_win():
    by = {x["symbol"]: x for x in evaluate(ranking_ds(), T_JUN)["ranked"]}
    pricey = by["ABT"]
    assert pricey["quality_score"] == 1.0 and pricey["value_score"] == 1 / 8
    assert pricey["rank"] > 1 and pricey["composite"] < by["AAPL"]["composite"] and pricey["rank"] > by["ACN"]["rank"]


def test_A04_negative_earnings_are_not_cheap_and_rank_low_on_quality():
    cos = F.ranking_set()
    neg = F.company("AMGN", "0000000901", price=30.0, ni=-1.0e9, ocf=2.0e9, assets=15e9, equity=7e9)       # loss-making, cash generative
    both = F.company("AMZN", "0000000902", price=30.0, ni=-1.0e9, ocf=-0.5e9, assets=15e9, equity=7e9)     # losses and cash burn
    out = evaluate(F.dataset(cos + [neg, both]), T_JUN)
    by = {x["symbol"]: x for x in out["ranked"]}
    snap = by["AMGN"]["snapshot"]
    assert snap["factors"]["earnings_yield"] is None and by["AMGN"]["factor_ranks"]["earnings_yield"] is None   # a negative P/E is missing, never "cheap"
    assert snap["factors"]["return_on_assets"] < 0 and by["AMGN"]["factor_ranks"]["return_on_assets"] == min(x["factor_ranks"]["return_on_assets"] for x in by.values())
    assert by["AMGN"]["value_score"] == pytest.approx((by["AMGN"]["factor_ranks"]["book_to_market"] + by["AMGN"]["factor_ranks"]["cash_flow_yield"]) / 2)
    assert "AMZN" not in by and out["summary"]["ineligible_by_reason"].get("TOO_FEW_VALUE_FACTORS") == 1   # only B/P left: below 2 of 3


def test_A05_missing_fundamentals_are_excluded_and_counted_never_filled():
    cos = F.ranking_set()
    no_assets = F.company("AMAT", "0000000903")
    no_ni = F.company("AMD", "0000000904")
    for y, q, s, e in F.quarters(2018, 2022):
        no_assets["overrides"][(F.ASSETS, e)] = None
        no_ni["overrides"][(F.NI, e)] = None
    cos = [c for c in cos if c["symbol"] not in ("AMAT", "AMD")] + [no_assets, no_ni]
    out = evaluate(F.dataset(cos), T_JUN)
    assert {x["symbol"] for x in out["ranked"]}.isdisjoint({"AMAT", "AMD"})
    assert out["summary"]["ineligible_by_reason"] == {"NO_ASSETS": 1, "NO_TTM_EARNINGS": 1}
    assert out["summary"]["universe"] == len(out["ranked"]) + 2 and set(out["summary"]["ineligible_by_reason"]) <= set(INELIGIBLE)


def test_A06_missing_equity_drops_its_factors_but_two_of_three_still_rank():
    cos = F.ranking_set()
    cos[3]["overrides"].update({(F.EQUITY, e): None for y, q, s, e in F.quarters(2018, 2022)})        # ACN reports no stockholders' equity
    by = {x["symbol"]: x for x in evaluate(F.dataset(cos), T_JUN)["ranked"]}
    f = by["ACN"]["snapshot"]["factors"]
    assert f["book_to_market"] is None and f["equity_ratio"] is None and by["ACN"]["factor_ranks"]["equity_ratio"] is None
    assert by["ACN"]["quality_score"] == pytest.approx((by["ACN"]["factor_ranks"]["return_on_assets"] + by["ACN"]["factor_ranks"]["cash_flow_on_assets"]) / 2)


def test_A07_percentile_ranks_are_bounded_tie_averaged_and_deterministic():
    r = pct_ranks([("B", 2.0), ("A", 1.0), ("C", 2.0), ("D", 5.0)])
    assert r == {"A": 0.25, "B": 0.625, "C": 0.625, "D": 1.0}
    assert pct_ranks([("X", 1e12), ("Y", 1.0), ("Z", 2.0)])["X"] == 1.0                           # an outlier is just the top rank: no domination
    ds = ranking_ds()
    a, b = evaluate(ds, T_JUN), evaluate(ds, T_JUN, symbols=list(reversed([s for s in VQ.RESEARCH_UNIVERSE if s in ds.bars])))
    strip = lambda o: [(x["symbol"], x["rank"], x["composite"], x["factor_ranks"]) for x in o["ranked"]]   # noqa: E731
    assert strip(a) == strip(b) and a["selected"] == b["selected"] and a["summary"] == b["summary"]   # same inputs, same selection, any order


def test_A08_composite_weights_and_top_n_come_from_the_core_parameters():
    ds = ranking_ds()
    data = BundleData(ds)
    syms = [s for s in VQ.RESEARCH_UNIVERSE if s in ds.bars]
    for vw in (0.4, 0.5, 0.6):
        out = ValueQualityStrategyCore(top_n=2, value_weight=vw).evaluate(T_JUN, syms, data, data.calendar)
        assert len(out["selected"]) == 2 and all(x["composite"] == pytest.approx(vw * x["value_score"] + (1 - vw) * x["quality_score"]) for x in out["ranked"])
    assert ValueQualityStrategyCore().top_n == VQ.TOP_N == 20 and ValueQualityStrategyCore().value_weight == VQ.VALUE_WEIGHT == 0.5


def test_A09_one_listing_per_issuer_and_financials_excluded():
    cos = F.ranking_set()
    cos.append(F.company("GOOG", "0000000905", symbols=["GOOG", "GOOGL"]))
    cos.append(F.company("GOOGL", "0000000905", symbols=["GOOG", "GOOGL"]))
    cos.append(F.company("JPM", "0000000906", sic=6021, sic_description="National Commercial Banks"))
    cos.append(F.company("KO", ""))                                                               # no SEC issuer
    out = evaluate(F.dataset(cos), T_JUN)
    ranked = {x["symbol"] for x in out["ranked"]}
    assert "GOOG" in ranked and "GOOGL" not in ranked and "JPM" not in ranked and "KO" not in ranked
    assert {k: out["summary"]["ineligible_by_reason"][k] for k in ("SECOND_LISTING", "EXCLUDED_SIC", "NO_CIK")} == {"SECOND_LISTING": 1, "EXCLUDED_SIC": 1, "NO_CIK": 1}


def test_A10_price_liquidity_and_size_filters():
    cos = F.ranking_set()
    cos.append(F.company("MU", "0000000907", price=4.0, shares=5e9))                               # below the 5 USD price floor
    cos.append(F.company("NFLX", "0000000908", price=6.0, shares=0.2e9))                           # 1.2B market cap
    out = evaluate(F.dataset(cos), T_JUN)
    assert out["summary"]["ineligible_by_reason"].get("PRICE_BELOW_MIN") == 1 and out["summary"]["ineligible_by_reason"].get("MARKET_CAP_BELOW_MIN") == 1
    assert out["ranked"] and all(x["snapshot"]["inputs"]["median_dollar_volume"] >= VQ.MIN_MEDIAN_DOLLAR_VOLUME for x in out["ranked"])


# ================================================================ P point in time =================================================================

def _refile_q2(fx, ds, symbol="AAPL", filed="2021-08-10"):
    for rows in fx[cik(ds, symbol)].values():
        for r in rows:
            if r["end"] == "2021-06-30":
                r["filed"] = filed


def test_P01_a_filing_on_aug_10_is_invisible_to_the_aug_rebalance_and_used_on_sep_1():
    base = ranking_ds()
    ds1 = with_facts(base, lambda fx: _refile_q2(fx, base))

    def modified(fx):
        _refile_q2(fx, base)
        for rows in fx[cik(base, "AAPL")].values():
            for r in rows:
                if r["end"] == "2021-06-30":
                    r["val"] = r["val"] * 10                                                      # a very different Q2
    ds2 = with_facts(base, modified)
    sessions = base.sessions
    assert "2021-08-02" in rebalance_sessions(sessions) and "2021-09-01" in rebalance_sessions(sessions)   # Aug 1 2021 is a Sunday
    assert BundleData(ds1).calendar.available_at("2021-08-10") == "2021-08-11"                    # usable from the session AFTER the filing
    a1, a2 = evaluate(ds1, "2021-08-02"), evaluate(ds2, "2021-08-02")
    key = lambda o: [(x["symbol"], x["rank"], x["composite"], x["snapshot"]["factors"]) for x in o["ranked"]]   # noqa: E731
    assert key(a1) == key(a2) and a1["selected"] == a2["selected"]                                 # Aug: Q1 or older only - Q2 changes nothing
    snap = {x["symbol"]: x for x in a1["ranked"]}["AAPL"]["snapshot"]
    assert snap["inputs"]["net_income_ttm"]["period_end"] == "2021-03-31" and snap["inputs"]["assets"]["period_end"] == "2021-03-31"
    assert all(v <= "2021-08-02" for v in snap["as_of"].values())
    s1 = {x["symbol"]: x for x in evaluate(ds1, "2021-09-01")["ranked"]}["AAPL"]["snapshot"]
    s2 = {x["symbol"]: x for x in evaluate(ds2, "2021-09-01")["ranked"]}["AAPL"]["snapshot"]
    assert s1["inputs"]["net_income_ttm"]["period_end"] == "2021-06-30" and s1["inputs"]["net_income_ttm"]["available_at"] == "2021-08-11"
    assert s2["inputs"]["assets"]["value"] == 10 * s1["inputs"]["assets"]["value"]                # Sep 1: the Q2 filing is used


def test_P02_a_restatement_is_invisible_until_its_filing_is_available():
    base = ranking_ds()
    c = cik(base, "AAPL")

    def restate(fx):
        q1 = next(r for r in fx[c][F.NI] if r["end"] == "2021-03-31")
        fx[c][F.NI].append({**q1, "val": q1["val"] * 0.25, "filed": "2021-08-10", "form": "10-Q/A", "accn": "RESTATED-Q1"})   # B, filed later
        q2a = next(r for r in fx[c][F.ASSETS] if r["end"] == "2021-06-30")
        fx[c][F.ASSETS].append({**q2a, "val": q2a["val"] * 2, "filed": "2021-08-20", "form": "10-Q/A", "accn": "RESTATED-Q2A"})
    ds = with_facts(base, restate)
    data = BundleData(ds)
    ni, assets, cal = data.facts("AAPL", "net_income"), data.facts("AAPL", "assets"), data.calendar
    before, after = pit.ttm(ni, "2021-08-02", cal), pit.ttm(ni, "2021-09-01", cal)
    q = F.company("AAPL", "x", ni=2.0e9)["ni"] / 4
    assert before["value"] == pytest.approx(4 * q) and "RESTATED-Q1" not in before["accn"]        # A: the original Q1
    assert after["value"] == pytest.approx(3 * q + 0.25 * q) and "RESTATED-Q1" in after["accn"]    # B: the restated Q1 (plus Q2)
    assert pit.instant(assets, "2021-08-20", cal)["value"] == 10e9 and pit.instant(assets, "2021-08-23", cal)["value"] == 20e9
    assert pit.instant(assets, "2021-08-23", cal)["accn"] == ["RESTATED-Q2A"]


def test_P03_nothing_filed_after_T_is_read_and_the_aux_cut_removes_it():
    ds = ranking_ds()
    cut = ds.until("2021-05-01")
    facts = cut.aux["events"]["facts"]
    assert all(str(r["filed"]) <= "2021-05-01" for tags in facts.values() for rows in tags.values() for r in rows)
    assert all(d <= "2021-05-01" for per in cut.aux["series"].values() for m in per.values() for d in m)
    assert cut.dataset_id == f"{ds.dataset_id}@until:2021-05-01" and cut.aux["static"] == ds.aux["static"]
    a = evaluate(ds, "2021-04-01")
    b = evaluate(cut, "2021-04-01")
    assert [(x["symbol"], x["composite"]) for x in a["ranked"]] == [(x["symbol"], x["composite"]) for x in b["ranked"]]   # the future is never needed


def test_P04_ttm_needs_four_contiguous_quarters_of_one_fiscal_year():
    cal = pit.Calendar(F.weekdays("2019-01-02", "2021-12-31"))
    def q(s, e, v, filed="2020-01-02"):
        return {"start": s, "end": e, "val": v, "filed": filed, "accn": e}
    four = [q("2019-01-01", "2019-03-31", 1), q("2019-04-01", "2019-06-30", 2), q("2019-07-01", "2019-09-30", 3), q("2019-10-01", "2019-12-31", 4)]
    assert pit.ttm(four, "2020-06-01", cal)["value"] == 10
    assert pit.ttm(four[:2] + four[3:], "2020-06-01", cal) is None                                 # a gap: never padded
    sixteen = [q("2019-01-01", "2019-03-24", 1), q("2019-03-25", "2019-06-16", 2), q("2019-06-17", "2019-09-08", 3), q("2019-09-09", "2019-12-29", 4)]   # a 16-week Q4
    assert pit.ttm(sixteen, "2020-06-01", cal)["value"] == 10
    ytd = [q("2019-01-01", "2019-03-31", 1), q("2019-01-01", "2019-06-30", 3), q("2019-01-01", "2019-09-30", 6), q("2019-01-01", "2019-12-31", 10)]   # cash flow: YTD only
    t = pit.ttm(ytd, "2020-06-01", cal)
    assert t["value"] == 10 and t["derived_quarters"] == 3
    assert pit.ttm(four, "2020-01-02", cal) is None                                                # filed 2020-01-02: available on the 3rd, not on the 2nd


def test_P05_stale_fundamentals_are_not_used():
    cos = F.ranking_set()
    late = F.company("AMGN", "0000000909")
    late["overrides"].update({(k, e): None for y, q, s, e in F.quarters(2020, 2022) for k in (F.NI, F.OCF, F.ASSETS, F.EQUITY, F.SHARES)})   # stopped filing after 2019
    out = evaluate(F.dataset(cos + [late]), T_JUN)
    assert "AMGN" not in {x["symbol"] for x in out["ranked"]} and out["summary"]["ineligible_by_reason"] == {"STALE_FUNDAMENTALS": 1}


# ================================================================ C corporate actions + concepts ==================================================

def test_C01_a_split_rebases_the_share_count_and_keeps_the_market_cap_continuous():
    cos = F.ranking_set()
    cos[7]["split"] = ("2021-06-15", 2.0)                                                          # AMD splits 2-for-1
    ds = F.dataset(cos)
    data = BundleData(ds)
    sh_before = pit.shares(data.facts("AMD", "share_count"), "2021-06-14", data.calendar, data.split_ratio("AMD"))
    sh_after = pit.shares(data.facts("AMD", "share_count"), "2021-07-01", data.calendar, data.split_ratio("AMD"))
    assert sh_before["split_factor"] == 1.0 and sh_after["split_factor"] == 2.0 and sh_after["filed"] < "2021-06-15"   # pre-split filing re-based
    assert sh_after["value"] == 2 * sh_after["reported"]
    sh_sep = pit.shares(data.facts("AMD", "share_count"), "2021-09-01", data.calendar, data.split_ratio("AMD"))
    assert sh_sep["filed"] > "2021-06-15" and sh_sep["split_factor"] == 1.0 and sh_sep["value"] == 2e9   # a post-split filing is already on the new basis
    m = lambda T: data.raw_close("AMD", T) * pit.shares(data.facts("AMD", "share_count"), T, data.calendar, data.split_ratio("AMD"))["value"]   # noqa: E731
    adj = {b.date: b.close for b in ds.bars["AMD"]}
    assert m("2021-07-01") / m("2021-06-14") == pytest.approx(adj["2021-07-01"] / adj["2021-06-14"], rel=1e-3)   # no jump at the split


def test_C02_a_share_count_filed_before_the_split_history_and_a_scale_error_are_unusable():
    ds = ranking_ds()
    snap = {s["symbol"]: s for s in [ValueQualityStrategyCore().snapshot("AAPL", "2019-02-01", BundleData(ds), BundleData(ds).calendar)]}
    assert snap["AAPL"]["reason"] == "NO_SHARE_COUNT"                                              # its split basis cannot be established

    def scale_error(fx):
        for r in fx[cik(ds, "AAPL")][F.SHARES]:
            r["val"] = 751.8                                                                       # millions reported as shares (the MCD case)
    bad = BundleData(with_facts(ds, scale_error))
    assert bad.facts("AAPL", "share_count") == []                                                  # rejected, never rescaled
    assert ValueQualityStrategyCore().snapshot("AAPL", T_JUN, bad, bad.calendar)["reason"] == "NO_SHARE_COUNT"


def test_C03_concept_priority_per_period_and_the_concept_is_recorded():
    base = ranking_ds()
    c = cik(base, "AAPL")
    alt = "NetIncomeLossAvailableToCommonStockholdersBasic"

    def switch(fx):
        rows = fx[c][F.NI]
        moved = [r for r in rows if r["end"] >= "2020-06-30"]
        fx[c][F.NI] = [r for r in rows if r["end"] < "2020-06-30"]
        fx[c][alt] = [dict(r) for r in moved]                                                       # the issuer switched concepts in mid-2020
        dup = next(r for r in fx[c][F.NI] if r["end"] == "2020-03-31")
        fx[c][alt].append({**dup, "val": dup["val"] * 9})                                          # both concepts for one period: the primary wins
    data = BundleData(with_facts(base, switch))
    t = pit.ttm(data.facts("AAPL", "net_income"), "2020-12-01", data.calendar)
    assert t["concept"] == sorted({F.NI, alt}) and t["quarters"] == ["2019-12-31", "2020-03-31", "2020-06-30", "2020-09-30"]
    assert t["value"] == pytest.approx(2.0e9)                                                      # the duplicate (x9) under the lower-priority concept is ignored
    assert [x for x in data.facts("AAPL", "net_income") if x["end"] == "2020-03-31" and x["concept"] == alt][0]["priority"] == 1


def test_C04_the_concept_lists_and_scale_rule_are_the_pre_registered_ones():
    assert VQ.CONCEPTS["net_income"][0] == "NetIncomeLoss" and VQ.CONCEPTS["assets"] == ("Assets",) and VQ.CONCEPTS["equity"] == ("StockholdersEquity",)
    assert VQ.MIN_SHARE_COUNT == 1_000_000 and VQ.QUARTER_DAYS == (80, 120) and VQ.TTM_SPAN_DAYS == (345, 385) and VQ.AVAILABILITY_LAG_SESSIONS == 1


# ================================================================ L the adapter ===================================================================

def test_L01_rebalances_are_the_first_session_of_each_month_after_the_liquidity_window():
    s = F.weekdays("2019-01-02", "2019-06-28")
    assert rebalance_sessions(s) == ["2019-02-01", "2019-03-01", "2019-04-01", "2019-05-01", "2019-06-03"]
    assert all(s.index(d) >= VQ.DOLLAR_VOLUME_SESSIONS for d in rebalance_sessions(s))


def test_L02_legs_are_the_core_selection_at_every_rebalance_and_factor_dates_precede_the_signal():
    ds, ad, run = full_run()
    data = BundleData(ds)
    syms = [s for s in VQ.RESEARCH_UNIVERSE if s in ds.bars]
    by_r = {}
    for c in run.candidates:
        by_r.setdefault(c.signal_date, set()).add(c.symbol)
    core = ValueQualityStrategyCore(top_n=5)
    for R_ in rebalance_sessions(ds.sessions):
        assert by_r.get(R_, set()) == set(core.evaluate(R_, syms, data, data.calendar)["selected"]), R_
    for c in run.candidates:
        assert all(d <= c.signal_date for d in c.feature_as_of.values()) and c.signal_date in rebalance_sessions(ds.sessions)
        assert all(p["filed"] < c.signal_date and p["available_at"] <= c.signal_date for p in c.features["fundamentals"].values())
        assert c.features["diluted_share_count"]["filed"] < c.signal_date and 1 <= c.features["rank"] <= 5
    assert len({(c.symbol, c.signal_date) for c in run.candidates}) == len(run.candidates)        # no duplicate entry


def test_L03_enter_hold_exit_flags_and_the_exit_at_the_right_rebalance():
    ds, ad, run = full_run()
    sched = rebalance_sessions(ds.sessions)
    legs = {(c.symbol, c.signal_date): c for c in run.candidates}
    hd = sorted(d for s, d in legs if s == "HD")
    assert hd and hd[0] == "2019-04-01"                                                            # the best stock from the first eligible rebalance
    last = hd[-1]
    gone = sched[sched.index(last) + 1]
    assert gone < "2021-03-01" and ("HD", gone) not in legs                                          # it leaves once its losses are filed
    out = evaluate(ds, gone, top_n=5)
    assert "HD" not in out["selected"] and "HD" in out["exit"] or "HD" not in [x["symbol"] for x in out["ranked"]][:5]
    c = legs[("HD", last)]
    assert c.exit_trade is True and legs[("HD", hd[0])].entry_trade is True and all(legs[("HD", d)].entry_trade is False for d in hd[1:])
    after = ds.bars_after("HD", last)
    assert after[c.max_hold_bars - 1].date == next(b.date for b in after if b.date > gone)          # the open of the first session after the exit rebalance
    t = next(t for t in run.arms["RULES"].trades if t.candidate_id == c.candidate_id)
    bar = ds.bar("HD", t.exit_date)
    assert t.exit_reason == "TIME_EXIT" and t.exit_raw == bar.open and t.exit_date > gone


def test_L04_the_last_rebalance_runs_to_the_data_end_and_purged_legs_are_the_boundary_month():
    ds, ad, run = full_run()
    sched = rebalance_sessions(ds.sessions)
    lastr = [c for c in run.candidates if c.signal_date == sched[-1]]
    assert lastr and all(c.exit_trade for c in lastr)
    ts = [t for t in run.arms["RULES"].trades if t.signal_date == sched[-1]]
    assert ts and all(t.exit_reason == "DATA_END" and t.exit_date == ds.sessions[-1] for t in ts)
    purged = run.boundary["purged"]["RULES"]
    cands = {c.candidate_id: c for c in run.candidates}
    assert run.boundary["train_boundary_purged_count"] == 5 and len({cands[i].signal_date for i in purged}) == 1


# ================================================================ H harness extensions ============================================================

def test_H01_stopless_equal_weight_legs_and_friction_only_on_real_orders():
    ds, ad, run = full_run()
    slip = run.friction_model["slippage_bps_per_side"] / 10000.0
    legs = {c.candidate_id: c for c in run.candidates}
    for t in run.arms["RULES"].trades:
        c = legs[t.candidate_id]
        assert t.stop is None and t.size_status == "EQUAL_WEIGHT" and t.qty * t.entry_fill <= 100000.0 * 0.2 + 1e-6
        assert t.entry_fill == pytest.approx(t.entry_raw * (1 + slip) if c.entry_trade else t.entry_raw)
        assert t.exit_fill == pytest.approx(t.exit_raw * (1 - slip) if c.exit_trade else t.exit_raw)
        if not c.entry_trade and not c.exit_trade:
            assert t.slippage_cost == 0 and t.fees == 0                                            # a continuation is not an order
    assert run.execution_assumptions["stop_policy"] == "NONE" and run.execution_assumptions["time_exit_fill"] == "OPEN"


def test_H02_a_stopless_strategy_needs_equal_weight_sizing():
    ds = ranking_ds()
    with pytest.raises(E.ValidationError, match="EQUAL_WEIGHT"):
        E.run_validation(ValueQualityAdapter(), ds, sizing={"mode": "RISK"}, registry=R.by_id(), created_at=AT)


def test_H03_the_dataset_id_covers_the_aux_data():
    ds = ranking_ds()
    other = with_facts(ds, lambda fx: fx[cik(ds, "AAPL")][F.NI][0].update(val=1.0))
    assert other.dataset_id != ds.dataset_id and Dataset(ds.bars, kind=ds.kind).dataset_id != ds.dataset_id
    rec = ds.provenance_record()
    assert rec["aux"] == {"series": ["raw_close", "raw_volume", "split_ratio"], "events": ["facts"], "static": ["issuers"]}


def test_H04_the_run_is_deterministic():
    ds = F.dataset(F.run_set())
    a = E.run_validation(ValueQualityAdapter({"top_n": 5}), ds, registry=R.by_id(), created_at=AT)
    b = E.run_validation(ValueQualityAdapter({"top_n": 5}), F.dataset(F.run_set()), registry=R.by_id(), created_at=AT)
    assert a.candidate_set_hash == b.candidate_set_hash and a.dataset_id == b.dataset_id
    assert a.arms["RULES"].metrics == b.arms["RULES"].metrics and a.diagnostics["strategy"] == b.diagnostics["strategy"]


def test_H05_arms_gated_equals_rules_and_model_is_not_evaluated():
    ds, ad, run = full_run()
    assert run.arms["GATED"].metrics == run.arms["RULES"].metrics and "NO_ADDITIONAL_GATES" in run.arms["GATED"].note
    assert run.arms["MODEL"].status == "NOT_EVALUATED" and run.arms["MODEL"].trades == []


def test_H06_train_vs_oos_reads_notional_R_without_the_risk_unit_band():
    from strategy_validation import diagnostics as DG
    tr, oo = {"trade_count": 1020, "expectancy_R": 0.017}, {"trade_count": 440, "expectancy_R": 0.021}
    risk = DG.train_vs_oos(tr, oo)                                                                 # stop-sized trades: 0.05 R is "near zero"
    assert risk["code"] == "CONSISTENTLY_WEAK" and risk["text"] == "TRAIN and OOS not clearly positive: no edge demonstrated."
    assert DG.train_vs_oos(tr, oo, nz=0.0)["code"] == "CONSISTENT"                                # equal-weight legs: R is the notional return
    assert DG.train_vs_oos(tr, {"trade_count": 440, "expectancy_R": -0.004}, nz=0.0)["code"] == "FAILURE_INSTABILITY"
    ds, ad, run = full_run()
    t, o = run.arms["RULES"].metrics["TRAIN"]["expectancy_R"], run.arms["RULES"].metrics["OOS"]["expectancy_R"]
    assert 0 < t < 0.05 and 0 < o < 0.05 and run.arms["RULES"].diagnostics["train_vs_oos"]["code"] == "CONSISTENT"


# ================================================================ U the independent strategy audit ================================================

def test_U01_the_real_record_passes_every_strategy_check():
    ds, ad, run = full_run()
    assert VA.audit(run.model_dump(), ds, top_n=5, value_weight=0.5) == []
    assert run.audit.status == "PASS" and "strategy-specific checks: 0 violation(s)" in run.audit.note


@pytest.mark.parametrize("check,tamper", [
    ("RANK_SELECTION", lambda c: c["features"].update(rank=6)),
    ("RANK_SELECTION", lambda c: c.update(score=c["score"] + 1.0)),
    ("FACTOR_AVAILABILITY", lambda c: c["feature_as_of"].update(fundamentals="2099-01-01")),
    ("FACTOR_AVAILABILITY", lambda c: c["features"]["fundamentals"]["assets"].update(filed=c["signal_date"])),
    ("RESTATEMENT", lambda c: c["features"]["fundamentals"]["assets"].update(value=c["features"]["fundamentals"]["assets"]["value"] * 1.5)),
    ("MARKET_CAP_SPLIT", lambda c: c["features"].update(market_cap=c["features"]["market_cap"] * 2)),
    ("TRANSACTION_FLAGS", lambda c: c.update(entry_trade=not c["entry_trade"])),
    ("EXIT_AT_REBALANCE", lambda c: c.update(max_hold_bars=c["max_hold_bars"] + 3)),
    ("REBALANCE_TIMESTAMP", lambda c: c.update(signal_date="2020-06-02")),
    ("SHARE_COUNT_SOURCE", lambda c: c["features"]["diluted_share_count"].update(reported=c["features"]["diluted_share_count"]["reported"] * 1.01)),
    ("SHARE_COUNT_SOURCE", lambda c: c["features"]["diluted_share_count"].update(period_end="2019-12-31", filed="2020-02-29")),
    ("FUNDAMENTAL_SOURCE", lambda c: c["features"]["fundamentals"]["net_income_ttm"].update(accn=["0000000000-99-BOGUS"])),
])
def test_U02_a_tampered_record_is_caught(check, tamper):
    ds, ad, run = full_run()
    rec = run.model_dump()
    victim = next(c for c in rec["candidates"] if c["signal_date"] == "2020-06-01" and c["features"]["fundamentals"].get("assets"))
    tamper(victim)
    assert check in {v["check"] for v in VA.audit(rec, ds, top_n=5, value_weight=0.5) if v["candidate_id"] == victim["candidate_id"]}


def test_U04_a_missing_leg_or_shuffled_ranks_are_caught():
    ds, ad, run = full_run()
    rec = run.model_dump()
    legs = [c for c in rec["candidates"] if c["signal_date"] == "2020-06-01"]
    rec["candidates"].remove(next(c for c in legs if c["features"]["rank"] == 3))                  # a selected stock silently dropped
    checks = {v["check"] for v in VA.audit(rec, ds, top_n=5, value_weight=0.5)}
    assert "PORTFOLIO_SIZE" in checks and "RANK_SELECTION" in checks
    rec = run.model_dump()
    a, b = [c for c in rec["candidates"] if c["signal_date"] == "2020-06-01" and c["features"]["rank"] in (1, 2)]
    a["features"]["rank"], b["features"]["rank"] = b["features"]["rank"], a["features"]["rank"]    # rank 1 now has the lower score
    assert any(v["check"] == "RANK_SELECTION" and "falling score" in v["detail"] for v in VA.audit(rec, ds, top_n=5, value_weight=0.5))


def test_U05_data_ending_on_a_rebalance_session_is_explicit_not_invalid():
    ds = F.dataset(F.run_set(), end="2021-12-01")                                               # 2021-12-01 is the first session of December
    assert ds.sessions[-1] in rebalance_sessions(ds.sessions)
    run = E.run_validation(ValueQualityAdapter({"top_n": 5}), ds, registry=R.by_id(), created_at=AT)
    assert run.audit.status == "PASS" and run.verdict["verdict"] != "INVALID", (run.audit.note, run.verdict)
    last = [c for c in run.candidates if c.signal_date == "2021-12-01"]
    skipped = [s for s in run.arms["RULES"].skipped if s.signal_date == "2021-12-01"] if hasattr(run.arms["RULES"], "skipped") else []
    assert last and (not skipped or all(s.reason == "NO_ENTRY_BAR" for s in skipped))
    assert all(t.exit_reason == "DATA_END" for t in run.arms["RULES"].trades if t.signal_date == "2021-11-01")


def test_U03_portfolio_size_and_the_audit_never_imports_the_core():
    ds, ad, run = full_run()
    rec = run.model_dump()
    rec["candidates"].append({**rec["candidates"][-1], "candidate_id": "extra", "symbol": "ZZZ"})
    assert any(v["check"] == "PORTFOLIO_SIZE" for v in VA.audit(rec, ds, top_n=len([c for c in rec["candidates"] if c["signal_date"] == rec["candidates"][-1]["signal_date"]]) - 1, value_weight=0.5))
    tree = ast.parse(now_text("value_quality/audit.py"))
    mods = {n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)} | {a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
    assert not any(m and m.startswith(("value_quality.core", "value_quality.pit", "value_quality.data", "value_quality.adapter")) for m in mods), mods
    assert "from value_quality import CONCEPTS, MIN_SHARE_COUNT" in now_text("value_quality/audit.py")   # the frozen rule constants only


def test_D01_the_universe_control_matches_a_strategy_that_holds_the_whole_universe_and_the_boundary_window_is_excluded():
    ds = ranking_ds()
    run = E.run_validation(ValueQualityAdapter({"top_n": 8}), ds, registry=R.by_id(), created_at=AT)   # 8 of 8 eligible: the strategy IS the control
    st = run.diagnostics["strategy"]
    bw = st["boundary_window"]
    assert run.boundary["train_boundary_purged_count"] == 8 and bw["sessions_excluded"] > 15 and bw["from"] <= run.train_end < bw["to"]
    for k in ("TRAIN", "OOS", "ALL"):
        p = st["portfolio"][k]
        per = p["excess_vs_universe_per_period"]
        assert abs(p["excess_vs_universe"]) < 0.005 and abs(per["mean"]) < 0.0002, (k, p)            # only integer shares and friction differ
        assert p["strategy"]["sessions"] == p["universe_equal_weight"]["sessions"] == p["benchmark"]["sessions"]   # the same sessions in all three


# ================================================================ G integrity gate + persistence ==================================================

def test_G01_a_research_strategy_is_replayable_but_registry_rules_still_apply():
    from strategy_validation.candidates import research_registry_error
    from strategy_validation.models import RawCandidate
    raw = RawCandidate(symbol="AAPL", signal_date="2021-06-01", entry_reference=10.0, invalidation_reference=None, target_reference=None, score=50.0,
                       features={}, feature_as_of={})
    reg = R.by_id()
    assert research_registry_error(ValueQualityAdapter(), raw, reg) == ""                          # RESEARCH: validated BEFORE any promotion
    fake = type("P", (ValueQualityAdapter,), {"strategy_id": "MOMENTUM"})()
    assert research_registry_error(fake, raw, reg).startswith("STRATEGY_NOT_IMPLEMENTED")
    assert research_registry_error(type("U", (ValueQualityAdapter,), {"strategy_id": "NOPE"})(), raw, reg).startswith("STRATEGY_UNKNOWN")
    assert research_registry_error(ValueQualityAdapter(), raw.model_copy(update={"direction": "SHORT"}), reg).startswith("DIRECTION_NOT_SUPPORTED")
    assert research_registry_error(type("Q", (ValueQualityAdapter,), {"source": "QUANTCONNECT"})(), raw, reg).startswith("SOURCE_MISMATCH")


def test_G02_candidate_contract_failures_block_persistence():
    from strategy_validation import integrity as IG
    ds, ad, run = full_run()
    rec = run.model_dump()
    assert {c.check: c.status for c in IG.evaluate(rec).checks}["CANDIDATE_CONTRACT"] == "PASS"
    rec["candidates_skipped"] = [{"candidate_id": "x", "symbol": "AAPL", "signal_date": "2021-06-01", "reason": "STANDARD_V1_INVALID",
                                  "detail": "STRATEGY_INACTIVE: VALUE_QUALITY is RESEARCH"}]
    el = IG.evaluate(rec)
    assert {c.check: c.status for c in el.checks}["CANDIDATE_CONTRACT"] == "FAIL" and el.status == "BLOCKED"


def test_G03_the_run_is_eligible_and_persists_with_its_flags(tmp_path, monkeypatch):
    monkeypatch.setenv("STOCK_AGENT_ENV", "test")                                                 # a SYNTHETIC run is stored only inside a test process
    from strategy_validation import service as SV
    from strategy_validation.store import StrategyValidationStore
    ds, ad, run = full_run()
    assert run.eligibility["status"] == "ELIGIBLE" and run.eligibility["integrity_status"] == "PASS"
    assert {c["check"]: c["status"] for c in run.eligibility["checks"]}["REPLAY_PARITY"] == "NOT_APPLICABLE"
    store = StrategyValidationStore(tmp_path / "v.db")
    store.insert(run)
    stored = store.latest("VALUE_QUALITY")
    d = SV.detail(stored, "VALUE_QUALITY", R.get("VALUE_QUALITY"))
    assert VQ.FLAG_SURVIVORSHIP in d["research_flags"] and "SELECTION_EDGE_NOT_ESTABLISHED" in d["research_flags"]   # displayed, never suppressed
    assert R.get("VALUE_QUALITY").status == "RESEARCH"                                             # a stored PASS never promotes


# ================================================================ S registry + Strategy Lab ========================================================

def test_S01_registry_entry_is_an_implemented_research_strategy_that_never_trades():
    from strategy_registry import service as S
    from strategy_registry.standard import check_signal
    e = R.get("VALUE_QUALITY")
    assert e.implemented and e.status == "RESEARCH" and e.version == VQ.STRATEGY_VERSION and e.direction_capability == "LONG" and e.source == "INTERNAL"
    assert e.time_horizon == "POSITION" and e.setup_type is None and not e.placeholder_note and e.asset_class == "US_EQUITY"
    v = S.registry_view({"available": False})
    assert [s["strategy_id"] for s in v["strategies"] if s["active"]] == ["DOWNSIDE_DISLOCATION_RECOVERY"]   # TRADE: still only DDR
    from test_product_flow_612 import signal
    r = check_signal(signal(strategy_id="VALUE_QUALITY"), R.by_id())
    assert r["valid"] is False and r["error"]["code"] == "STRATEGY_INACTIVE"                       # a live signal is refused
    assert S.derive_lifecycle(e, {"available": True, "mode": "PAPER_AUTO", "validation": {"sessions_completed": 9}})["status"] == "RESEARCH"


def test_S02_the_strategy_lab_detail_has_the_thirteen_sections_and_no_invented_result():
    from strategy_registry import service as S
    d = S.detail_view("VALUE_QUALITY", {"available": False}, validation={"validation_status": "NOT_RUN"})
    keys = [s["key"] for s in d["sections"]]
    assert keys == ["overview", "hypothesis", "rules", "data_requirements", "entry_logic", "invalidation", "exit_logic", "holding_horizon", "backtest",
                    "out_of_sample", "risk_notes", "paper_results", "decision_log"]
    st = {s["key"]: s for s in d["sections"]}
    assert st["paper_results"]["state"] == "NOT_EVALUATED" and st["decision_log"]["state"] == "NOT_EVALUATED"
    assert d["links"] == {"design_docs": "DESIGN_64_VALUE_QUALITY.md"} and "SURVIVORSHIP_BIAS_PRESENT" in " ".join(st["risk_notes"]["items"])
    text = json.dumps(d["sections"])
    assert not re.search(r"\d+(\.\d+)?\s?%", text) and "Sharpe" not in text                       # results live in the Validation block only
    assert st["overview"]["facts"]["top_n"] == 20 and st["overview"]["facts"]["value_weight"] == 0.5


def test_S03_the_registry_package_stays_display_only():
    src = now_text("strategy_registry/value_quality.py")
    tree = ast.parse(src)
    mods = {n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)} | {a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
    assert mods <= {"__future__", "typing", "strategy_registry", "strategy_registry.models", "value_quality"}, mods
    for forbidden in ("requests", "urllib", "socket", "sqlite3", "broker", "execution", "submit", "order("):
        assert forbidden not in src, forbidden


def test_S04_the_not_run_reason_without_a_stored_run_is_honest():
    from strategy_validation import service as SV
    s = SV.summary(None, "VALUE_QUALITY", R.get("VALUE_QUALITY"))
    assert s["validation_status"] == "NOT_RUN" and "--persist" in s["not_run_reason"] and s["research_flags"] == []


def test_S05_node_tests_of_the_flags_and_the_portfolio_table():
    r = subprocess.run(["node", "--test", str(ROOT / "tests" / "frontend" / "value_quality.test.cjs")], capture_output=True, text=True, cwd=ROOT)
    assert r.returncode == 0 and re.search(r"pass 4\b", r.stdout) and re.search(r"fail 0\b", r.stdout), r.stdout[-3000:] + r.stderr[-2000:]


# ================================================================ X isolation + CLI ===============================================================

def test_X01_a_replay_opens_no_socket(monkeypatch):
    def refuse(*a, **k):
        raise AssertionError("network access during a replay")
    monkeypatch.setattr(socket, "socket", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)
    run = E.run_validation(ValueQualityAdapter({"top_n": 5}), F.dataset(F.run_set()), registry=R.by_id(), created_at=AT)
    assert run.candidate_count > 0


def test_X02_no_broker_execution_or_network_import_outside_the_ingest_module():
    for f in ("__init__.py", "adapter.py", "audit.py", "core.py", "data.py", "diagnostics.py", "pit.py"):
        tree = ast.parse(now_text(f"value_quality/{f}"))
        mods = {n.module or "" for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)} | {a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
        assert not any(m.startswith(("execution", "safety", "paper", "portfolio", "proposals", "api", "requests", "urllib", "httpx", "socket", "alpaca",
                                     "value_quality.ingest", "anthropic")) for m in mods), (f, mods)
    ing = now_text("value_quality/ingest.py")
    assert "def fetch(" in ing and "def build(" in ing and not re.search(r"/v2/orders|submit_order|place_order|\.post\(", ing)
    assert "alpaca.data.historical" in ing and "alpaca.trading" not in ing and "TradingClient" not in ing   # market data only, never the trading API
    for rel in ("strategy_validation/engine.py", "strategy_validation/adapters.py", "value_quality/adapter.py"):
        assert "ingest" not in now_text(rel), rel                                                  # the replay never reaches the network module


def test_X03_cli_validates_and_persists_value_quality(tmp_path, monkeypatch):
    monkeypatch.setenv("STOCK_AGENT_ENV", "test")
    from strategy_validation import cli
    p = tmp_path / "vq.json"
    dump(F.dataset(F.run_set()), str(p))
    out = io.StringIO()
    code = cli.main(["validate", "--strategy", "VALUE_QUALITY", "--dataset", str(p), "--db", str(tmp_path / "v.db"), "--persist", "--output", str(tmp_path / "run.json")], out=out)
    text = out.getvalue()
    assert code == 0 and text.startswith("VALUE_QUALITY v1.0:") and "stored run" in text and "integrity: PASS - persistence ELIGIBLE" in text, text
    rec = json.loads((tmp_path / "run.json").read_text(encoding="utf-8"))
    assert rec["research_flags"] and VQ.FLAG_SURVIVORSHIP in rec["research_flags"] and rec["config_snapshot"]["adapter"]["stop_policy"] == "NONE"
    out2 = io.StringIO()
    assert cli.main(["runs", "--strategy", "VALUE_QUALITY", "--db", str(tmp_path / "v.db")], out=out2) == 0 and "VALUE_QUALITY v1.0" in out2.getvalue()


# ================================================================ Z pre-registration, documents, frozen controls ==================================

def test_Z01_the_pre_registered_rules():
    assert VQ.STRATEGY_VERSION == "1.0" and 10 <= VQ.TOP_N <= 30 and VQ.VALUE_WEIGHT + VQ.QUALITY_WEIGHT == 1.0 and VQ.REBALANCE == "MONTHLY"
    assert VQ.VALUE_FACTORS == ("earnings_yield", "book_to_market", "cash_flow_yield") and VQ.QUALITY_FACTORS == ("return_on_assets", "cash_flow_on_assets", "equity_ratio")
    assert VQ.SENSITIVITY == ({"top_n": 15}, {"top_n": 25}, {"value_weight": 0.4}, {"value_weight": 0.6}) and VQ.MIN_FACTORS_PER_GROUP == 2
    r = subprocess.run(["git", "show", "4358a3a:value_quality/__init__.py"], cwd=ROOT, capture_output=True, text=True)
    if r.returncode == 0:                                                                          # the pre-registration commit (before the OOS run)
        frozen = {}
        exec(compile(r.stdout, "frozen", "exec"), frozen)                                           # noqa: S102 - the repository's own file at a fixed commit
        for k in ("TOP_N", "VALUE_WEIGHT", "QUALITY_WEIGHT", "VALUE_FACTORS", "QUALITY_FACTORS", "MIN_FACTORS_PER_GROUP", "MIN_PRICE", "MIN_MARKET_CAP",
                  "MIN_MEDIAN_DOLLAR_VOLUME", "MAX_FUNDAMENTAL_AGE_DAYS", "AVAILABILITY_LAG_SESSIONS", "EXCLUDED_SIC", "RESEARCH_UNIVERSE", "CONCEPTS",
                  "MIN_SHARE_COUNT", "QUARTER_DAYS", "TTM_SPAN_DAYS", "SENSITIVITY"):
            assert frozen[k] == getattr(VQ, k), k                                                  # nothing tuned after the OOS result


def test_Z02_documents_state_the_method_the_result_and_the_limits():
    d = now_text("DESIGN_64_VALUE_QUALITY.md")
    for s in ("## 1. Feasibility", "| FACTOR | SOURCE | FIELD | POINT-IN-TIME? | AVAILABLE DATE | COVERAGE | REVISIONS? | USABLE? | NOTES |",
              "## 2. Point in time", "## 3. Universe and survivorship", "SURVIVORSHIP_BIAS_PRESENT", "## 4. Corporate actions", "## 6. Factors",
              "## 9. Portfolio and rebalance", "## 11. Validation", "## 13. Results", "SELECTION_EDGE_NOT_ESTABLISHED", "## 15. Adversarial review",
              "## 16. Limitations", "PASS means eligible for human review", "Backtest results are hypothetical.", "pre-registration"):
        assert s in d, s
    readme = now_text("README.md")
    sec = re.findall(r"(?ms)^## Value \+ Quality strategy \(Stage 6\.4, research only\)\n.*?(?=^## )", readme)
    assert len(sec) == 1 and "SURVIVORSHIP_BIAS_PRESENT" in sec[0] and "python -m strategy_validation validate --strategy VALUE_QUALITY" in sec[0]
    assert readme.index("## Strategy validation harness (Stage 6.2") < readme.index("## Value + Quality strategy (Stage 6.4") < readme.index("## Continuous integration (Stage 6.0.3")
    h = now_text("STRATEGY_VALIDATION_HARNESS.md")
    for s in ("stop_policy", "EQUAL_WEIGHT", "time_exit_fill", "entry_trade", "CANDIDATE_CONTRACT", "aux"):
        assert s in h, s
    assert not re.search(r"(?i)\b(guaranteed|buy now|sell now)\b", d + sec[0])


def test_Z03_frozen_controls_and_execution_modes_unchanged():
    import execution as X
    import forward_validation as FV
    assert FV.frozen_hash() == FROZEN_HASH and X.MODES == ("MONITOR_ONLY", "PAPER_AUTO")
    groups = json.loads(now_text(".github/ci/pytest_groups.json"))
    assert any("test_value_quality_64.py" in g["files"] for g in groups["groups"].values()) if isinstance(groups.get("groups"), dict) else "test_value_quality_64.py" in now_text(".github/ci/pytest_groups.json")
