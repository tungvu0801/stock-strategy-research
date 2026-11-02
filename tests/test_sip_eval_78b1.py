"""
Stage 7.8B-1 - the isolated SIP research dataset builder, the NYSE calendar, the pre-run data-quality report, the evaluation manifest and the
offline V1 / C1 evaluator (research_data/, swing_eval/; DESIGN_78B1_SIP_EVALUATION.md). SYNTHETIC data only: no network, no real file, no
database; every output under tmp_path. Operator decision lock: STAGE_7_8B1_DECISION_LOCK.md (confirmed 2026-11-02).

  A01-A02 the locked values; the strategy's fingerprint untouched
  C01-C04 the NYSE calendar 2016-2026: published session counts, the IEX-bundle closures, the comparison, autorun's 2026 table
  B01-B09 the SIP builder: the plan, the approval gate, the fetch (pagination, page guard, no retry, no credential stored), the build, its
          refusals, the IEX path untouched, the CLI, the real transport (no redirect), the credentials
  Q01-Q06 the pre-run report: CLEAN, every BLOCK, every FINDING (holes, raw gaps, zero volume, Stage 6.7 inconsistency, M-7), the approval gate,
          no strategy computation, the requested window, raw volume gaps
  M01-M05 the manifest: build / verify, tampering, pre-checks, post-checks (M-2 / M-3), the CLI
  E01-E10 the evaluator: end to end, refusals (R17, approval, pre-checks), the offline guard, an INVALID run, M-5 blocking (base and neighbour),
          SIP only under the report's provenance, the CLI, INVALID with a blocking finding, the real tree check
  N01-N06 the analysis: R9 stability, R10 comparison, the bootstrap, exposure, R11 eligibility, the stop and integrity findings
  X01-X02 isolation
  G01     the documents
  F01-F03 scope (freeze bookkeeping)
"""
from __future__ import annotations

import ast
import copy
import functools
import gzip
import hashlib
import json
import math
import os
import socket
import sqlite3
import subprocess
import sys
import types
from datetime import date, datetime, timezone
from pathlib import Path

import pytest

import freeze_ledger as FL
import research_data as RD
import sip_eval_fixtures as SF
import swing_eval as SE
import swing_pullback as SP
import swing_pullback_fixtures as SPF
from research_data import nyse_calendar as CAL
from research_data import quality as Q
from research_data import sip_bundle as SB
from strategy_validation.dataset import Dataset, dump, load
from swing_eval import analysis as AN
from swing_eval import evaluate as EV
from swing_eval import manifest as MF
from swing_pullback import plan as PL

ROOT = Path(__file__).resolve().parents[1]
AT = "2026-11-02T00:00:00+00:00"
OK_REF = "OPERATOR-APPROVAL:test-only-reference"
U = ["AAPL", "ABBV", "ABT", "GOOG", "GOOGL", "SPY"]


# ============================================================================================================ helpers
@functools.lru_cache(maxsize=None)
def base():
    return SF.sip_dataset(6)


def write(tmp_path, ds, name="ds.json"):
    tmp_path.mkdir(parents=True, exist_ok=True)
    p = tmp_path / name
    dump(ds, str(p))
    return p


def package(tmp_path, ds, *, provenance=None, report_approval=None):
    """(dataset path, report, manifest path, report path) for a synthetic dataset."""
    dp = write(tmp_path, ds)
    rep = Q.report(str(dp), provenance=provenance, approval=report_approval)
    m = MF.build(dataset_path=str(dp), report=rep, code_commit="c0ffee", created_at=AT, evidence_location=str(tmp_path / "ev"), provenance=provenance)
    mp, rp = tmp_path / "manifest.json", tmp_path / "report.json"
    mp.write_text(json.dumps(m), encoding="utf-8")
    rp.write_text(json.dumps(rep), encoding="utf-8")
    return dp, rep, mp, rp


def run_eval(tmp_path, ds, **kw):
    dp, rep, mp, rp = package(tmp_path, ds)
    out = tmp_path / "ev"
    s = EV.evaluate(str(mp), str(dp), str(rp), str(out), commit_fn=lambda: "c0ffee", tree_clean_fn=lambda: True, **kw)
    return s, out


@functools.lru_cache(maxsize=None)
def e2e(tmp_root: str):
    return run_eval(Path(tmp_root), base()[0])


def checks(rep, code=None, severity=None):
    return [c for c in rep["checks"] if (code is None or c["code"] == code) and (severity is None or c["severity"] == severity)]


def fetched(tmp_path, *, universe=U, split=None, tickers=None, statuses=None, extra_symbol=None, monkeypatch=None, page_limit=None, end="2016-04-29"):
    if page_limit and monkeypatch:
        monkeypatch.setattr(SB, "PAGE_LIMIT", page_limit)
    plan = SB.request_plan("2016-01-04", end, universe=universe)
    sess = CAL.sessions("2016-01-04", end)
    bars = SF.synthetic_bars(universe, sess, split=split)
    tr = SF.fake_provider(bars, tickers if tickers is not None else SF.sec_tickers(universe, shared={"GOOGL": "GOOG"}), statuses=statuses,
                          extra_symbol=extra_symbol)
    out = tmp_path / "sip"
    sleeps = []
    path = SB.fetch(plan, out=out, approval=OK_REF, plan_sha256=SB.sha256_of(plan), transport=tr, alpaca_key=SF.KEY, alpaca_secret=SF.SECRET,
                    sec_user_agent=SF.UA, sleep=sleeps.append)
    return plan, out, tr, bars, sess, sleeps, path


# ============================================================================================================ the lock (A)
def test_A01_the_locked_values_are_the_confirmed_decision_lock():
    assert SE.R9 == {"fold_min_trades": 20, "min_evaluable_folds": 5, "positive_share": [2, 3], "median_fold_mean_r_min": 0.05,
                     "max_fold_share_of_net": 0.80, "gate": "V1 BASE", "reported": ["C1 BASE", "V1 STRESS_2X", "C1 STRESS_2X"]}
    r = SE.R10
    assert (r["segment"], r["r"], r["delta_r_min"], r["both_costs"], r["min_oos_trades"], r["min_trades"], r["paired_fold_min_trades"],
            r["min_paired_folds"], r["ahead_share"], r["fold_run"]) == ("OOS", "r_net", 0.05, True, 20, 30, 20, 4, [2, 3], "BASE")
    assert r["bootstrap"] == {"block": "calendar_month_of_signal", "joint": True, "resamples": 2000, "seed": 78, "percentiles": [5, 50, 95],
                              "role": "descriptive"}
    text = " ".join(r["interpretation"])
    assert "not a significance test" in text and "includes TRAIN observations" in text and "NO_ADDED_VALUE means only" in text
    f = SE.R12
    assert (f["treatment"], f["fee_per_share"], f["flag"]) == ("NOT_MODELLED", 0.0, "REGULATORY_FEES_NOT_MODELLED")
    est = f["illustrative_estimate"]
    assert est["share_price_usd"] == 50.0 and "not a verified bound" in est["note"] and "not representative of every share price" in est["note"]
    sec_bps = 20.60 / 1e6 * 1e4
    for price, bps in est["taf_bps_by_price"].items():
        assert bps == pytest.approx(0.000195 / float(price) * 1e4, abs=0.001), price            # TAF is per share: the estimate depends on the price
    assert round(sec_bps + est["taf_bps_by_price"]["50"], 2) == est["bps_of_sell_notional"] == 0.25
    assert f["missing"] == ["SEC Section 31 rates 2016 -> 2025-05-13", "FINRA TAF rates 2016-2025", "CAT fee schedule"]
    assert SE.R13 == {"commission_per_order": 0.0, "status": "ASSUMPTION", "verify": "Alpaca's published fee schedule, before any real run"}
    assert (SE.TRAIN_RATIO, SE.SIZING, SE.ARMS) == (0.70, {"equity": 100000.0, "risk_fraction": 0.005, "max_position_pct": 10.0}, ["RULES"])
    assert SE.EXECUTION == {"entry_policy": "NEXT_BAR_OPEN", "ambiguous_bar_policy": "STOP_FIRST", "time_exit_fill": "CLOSE", "stop_policy": "INVALIDATION"}
    assert (RD.FEED, RD.API_FEED, RD.DEFAULT_START, RD.ADJUSTMENTS) == ("SIP", "sip", "2016-01-04", ("all", "raw", "split"))
    assert RD.ALLOWED_URLS == ("https://data.alpaca.markets/v2/stocks/bars", "https://www.sec.gov/files/company_tickers.json")
    assert (RD.PAGE_LIMIT, RD.MIN_INTERVAL_S, RD.DAILY_BAR_STAMPS) == (10_000, 0.35, ("T04:00:00Z", "T05:00:00Z"))


def test_A02_the_strategy_fingerprint_and_package_are_untouched():
    assert SP.preregistration_hash() == "6c20fdb4f9c093fed0a82c21c642dfceee2cc6d424104390c7c432e3cfa61903"
    assert PL.RUNS == (("V1", "BASE"), ("V1", "STRESS_2X"), ("C1", "BASE"), ("C1", "STRESS_2X"))


# ============================================================================================================ the calendar (C)
def test_C01_the_table_gives_the_published_nyse_session_counts():
    assert {y: len(CAL.sessions(f"{y}-01-01", f"{y}-12-31")) for y in range(2016, 2026)} == {
        2016: 252, 2017: 251, 2018: 251, 2019: 252, 2020: 253, 2021: 252, 2022: 251, 2023: 250, 2024: 252, 2025: 250}
    assert "2018-12-05" in CAL.CLOSURES and "2025-01-09" in CAL.CLOSURES and "2021-12-31" not in CAL.CLOSURES   # 2022-01-01 was a Saturday
    assert "2021-06-18" not in CAL.CLOSURES and "2022-06-20" in CAL.CLOSURES                                    # Juneteenth from 2022
    assert not [d for d in CAL.CLOSURES if date.fromisoformat(d).weekday() >= 5]
    assert not set(CAL.EARLY_CLOSES) & set(CAL.CLOSURES)


AUDIT_CLOSURES_2020_2026 = [
    "2020-09-07", "2020-11-26", "2020-12-25",
    "2021-01-01", "2021-01-18", "2021-02-15", "2021-04-02", "2021-05-31", "2021-07-05", "2021-09-06", "2021-11-25", "2021-12-24",
    "2022-01-17", "2022-02-21", "2022-04-15", "2022-05-30", "2022-06-20", "2022-07-04", "2022-09-05", "2022-11-24", "2022-12-26",
    "2023-01-02", "2023-01-16", "2023-02-20", "2023-04-07", "2023-05-29", "2023-06-19", "2023-07-04", "2023-09-04", "2023-11-23", "2023-12-25",
    "2024-01-01", "2024-01-15", "2024-02-19", "2024-03-29", "2024-05-27", "2024-06-19", "2024-07-04", "2024-09-02", "2024-11-28", "2024-12-25",
    "2025-01-01", "2025-01-09", "2025-01-20", "2025-02-17", "2025-04-18", "2025-05-26", "2025-06-19", "2025-07-04", "2025-09-01", "2025-11-27",
    "2025-12-25",
    "2026-01-01", "2026-01-19", "2026-02-16", "2026-04-03", "2026-05-25", "2026-06-19", "2026-07-03", "2026-09-07"]


def test_C02_the_table_reproduces_the_iex_bundles_60_closures_and_1570_sessions():
    in_span = sorted(d for d in CAL.CLOSURES if "2020-07-27" <= d <= "2026-10-23")
    assert in_span == AUDIT_CLOSURES_2020_2026 and len(in_span) == 60                    # STAGE_7_8C_REAL_DATA_READINESS_AUDIT.md section 3.6
    assert len(CAL.sessions("2020-07-27", "2026-10-23")) == 1570


def test_C03_the_comparison_names_extra_missing_weekend_and_uncovered_dates():
    s = CAL.sessions("2016-01-04", "2016-12-30")
    assert CAL.compare(s)["status"] == "MATCH"
    r = CAL.compare(s + ["2016-01-09"])                                                  # a Saturday
    assert r["status"] == "MISMATCH" and r["weekend"] == ["2016-01-09"] and r["extra"] == ["2016-01-09"]
    r = CAL.compare(s + ["2016-07-04"])                                                  # a closure
    assert r["extra"] == ["2016-07-04"] and r["closures"] == ["2016-07-04"]
    r = CAL.compare([d for d in s if d != "2016-06-15"])
    assert r["missing"] == ["2016-06-15"]
    assert CAL.compare(["2015-12-31", "2016-01-04"])["status"] == "NOT_COVERED"
    with pytest.raises(CAL.CalendarError):
        CAL.sessions("2027-01-04", "2027-01-08")


def test_C04_the_2026_closures_and_early_closes_agree_with_autorun():
    from autorun import calendar as AC
    assert {d.isoformat() for d in AC.HOLIDAYS if d.year == 2026} == {d for d in CAL.CLOSURES if d.startswith("2026")}
    assert {d.isoformat() for d in AC.EARLY_CLOSES if d.year == 2026} == {d for d in CAL.EARLY_CLOSES if d.startswith("2026")}


# ============================================================================================================ the SIP builder (B)
def test_B01_the_plan_lists_every_request_one_exclusive_window_and_nothing_broader():
    p = SB.request_plan("2016-01-04", "2026-10-30")
    bars = [r for r in p["requests"] if r["kind"] == "BARS"]
    assert p["requests"][0] == {"name": "sec_company_tickers", "kind": "SEC", "url": RD.SEC_TICKERS_URL, "params": {}} and len(bars) == 15
    for r in bars:
        assert r["url"] == RD.BARS_URL and r["params"]["feed"] == "sip" and r["params"]["timeframe"] == "1Day" and r["params"]["limit"] == 10_000
        assert (r["params"]["start"], r["params"]["end"]) == ("2016-01-04T00:00:00Z", "2026-10-31T00:00:00Z")      # [start, day after end)
        assert len(r["symbols"]) <= 20
    for adj in RD.ADJUSTMENTS:
        syms = [s for r in bars if r["adjustment"] == adj for s in r["symbols"]]
        assert syms == list(RD.RESEARCH_UNIVERSE) + ["SPY"]                              # each symbol once per adjustment
    assert SB.sha256_of(p) == SB.sha256_of(SB.request_plan("2016-01-04", "2026-10-30")) != SB.sha256_of(SB.request_plan("2016-01-04", "2026-10-29"))
    with pytest.raises(ValueError):
        SB.request_plan("2017-01-04", "2016-01-04")
    assert SB.request_plan()["end"] < datetime.now(timezone.utc).date().isoformat()                       # completed sessions only


def test_B02_a_fetch_is_refused_before_any_request_without_approval_plan_hash_scope_or_credentials(tmp_path):
    plan = SB.request_plan("2016-01-04", "2016-03-31", universe=U)
    sha = SB.sha256_of(plan)
    tr = SF.fake_provider({"all": {}, "raw": {}, "split": {}}, {})
    kw = dict(out=tmp_path / "o", approval=OK_REF, plan_sha256=sha, transport=tr, alpaca_key=SF.KEY, alpaca_secret=SF.SECRET, sec_user_agent=SF.UA,
              sleep=lambda s: None)
    bad_url = copy.deepcopy(plan)
    bad_url["requests"][2]["url"] = "https://paper-api.alpaca.markets/v2/orders"
    iex = copy.deepcopy(plan)
    iex["requests"][1]["params"]["feed"] = "iex"
    broad = copy.deepcopy(plan)
    broad["requests"][1]["params"]["symbols"] += ",XYZ"
    longer = copy.deepcopy(plan)
    longer["requests"][3]["params"]["end"] = "2026-10-31T00:00:00Z"                     # one request's window wider than the plan's
    current = SB.request_plan("2016-01-04", "2026-11-02", universe=U)                   # ends today: the session may be incomplete
    sec_params = copy.deepcopy(plan)
    sec_params["requests"][0]["params"] = {"q": "x"}
    extra = copy.deepcopy(plan)
    extra["requests"][1]["params"]["asof"] = "2016-01-04"
    swapped = copy.deepcopy(plan)
    swapped["requests"][1]["symbols"] = ["AAPL"]                                         # the archived symbol list no longer matches the request
    windowless = {k: v for k, v in plan.items() if k != "window"}
    cases = [(plan, dict(approval=None)), (plan, dict(approval="approved")), (plan, dict(approval="OPERATOR-APPROVAL:")), (plan, dict(plan_sha256="0" * 64)),
             (plan, dict(approval="APPROVED-BY-THE-OPERATOR-REFERENCE-123")),
             (dict(plan, builder="other"), dict(plan_sha256=SB.sha256_of(dict(plan, builder="other")))), (plan, dict(alpaca_key="")),
             (plan, dict(sec_user_agent="")), (bad_url, dict(plan_sha256=SB.sha256_of(bad_url))), (iex, dict(plan_sha256=SB.sha256_of(iex))),
             (broad, dict(plan_sha256=SB.sha256_of(broad))), (longer, dict(plan_sha256=SB.sha256_of(longer))),
             (current, dict(plan_sha256=SB.sha256_of(current), now=lambda: "2026-11-02T20:59:00+00:00")),
             (sec_params, dict(plan_sha256=SB.sha256_of(sec_params))), (extra, dict(plan_sha256=SB.sha256_of(extra))),
             (swapped, dict(plan_sha256=SB.sha256_of(swapped))), (windowless, dict(plan_sha256=SB.sha256_of(windowless))),
             (plan, dict(out=ROOT / "sip_fetch_test_78b1")), (plan, dict(out=tmp_path / "raw.db"))]
    for p, over in cases:
        with pytest.raises(SB.FetchRefused):
            SB.fetch(p, **{**kw, **over})
    assert tr.calls == [] and not (tmp_path / "o").exists() and not (ROOT / "sip_fetch_test_78b1").exists()
    if os.name == "nt":                                                                  # another spelling of a repository path is caught too
        with pytest.raises(SB.FetchRefused, match="inside the repository"):
            SB.fetch(plan, **{**kw, "out": Path("\\\\?\\" + str(ROOT / "sip_fetch_test_78b1"))})
        assert tr.calls == []
    (tmp_path / "full").mkdir()
    (tmp_path / "full" / "x").write_text("x")
    with pytest.raises(SB.FetchRefused, match="never overwrites"):
        SB.fetch(plan, **{**kw, "out": tmp_path / "full"})


def test_B03_the_fetch_follows_pages_keeps_every_response_paces_requests_and_stores_no_credential(tmp_path, monkeypatch):
    plan, out, tr, bars, sess, sleeps, path = fetched(tmp_path, monkeypatch=monkeypatch, page_limit=50)
    man = json.loads(path.read_text(encoding="utf-8"))
    assert man["complete"] and man["plan_sha256"] == SB.sha256_of(plan) and man["approval"] == OK_REF
    per_req = {}
    for r in man["responses"]:
        per_req[r["name"]] = per_req.get(r["name"], 0) + 1
        body = gzip.decompress((out / "raw" / r["file"]).read_bytes())
        assert hashlib.sha256(body).hexdigest() == r["sha256"]
    expect_pages = math.ceil(len(U) * len(sess) / 50)
    assert per_req["sec_company_tickers"] == 1 and all(per_req[f"bars_{a}_00"] == expect_pages for a in RD.ADJUSTMENTS)
    assert len(tr.calls) == 1 + 3 * expect_pages and all(c["url"] in RD.ALLOWED_URLS for c in tr.calls)
    assert [c["params"].get("page_token") for c in tr.calls[1:expect_pages + 1]][1:] == [str(50 * k) for k in range(1, expect_pages)]
    assert sleeps and all(0 < s <= RD.MIN_INTERVAL_S for s in sleeps)
    blob = path.read_bytes() + b"".join(gzip.decompress(p.read_bytes()) for p in (out / "raw").iterdir())
    assert SF.KEY.encode() not in blob and SF.SECRET.encode() not in blob                 # credentials are never stored


def test_B03b_a_failed_request_or_too_many_pages_stops_the_fetch_without_retry(tmp_path, monkeypatch):
    with pytest.raises(SB.FetchRefused, match="HTTP 500"):
        fetched(tmp_path, statuses={2: 500})
    man = json.loads((tmp_path / "sip" / "manifest.json").read_text(encoding="utf-8"))
    assert man["complete"] is False and "HTTP 500" in man["stopped"]
    monkeypatch.setattr(SB, "MAX_PAGES_PER_REQUEST", 3)
    with pytest.raises(SB.FetchRefused, match="materially broader"):
        fetched(tmp_path / "b", monkeypatch=monkeypatch, page_limit=50)
    with pytest.raises(SB.BuildError, match="incomplete"):
        SB.build(tmp_path / "b" / "sip", kind="SYNTHETIC")
    plan = SB.request_plan("2016-01-04", "2016-03-31", universe=U)
    real = SF.fake_provider(SF.synthetic_bars(U, CAL.sessions("2016-01-04", "2016-03-31")), SF.sec_tickers(U))
    calls = []

    def flaky(url, params, headers):
        calls.append(url)
        if len(calls) == 3:
            raise ConnectionError("reset by " + SF.SECRET)                             # an exception text is never written
        return real(url, params, headers)
    with pytest.raises(SB.FetchRefused, match="ConnectionError"):
        SB.fetch(plan, out=tmp_path / "c", approval=OK_REF, plan_sha256=SB.sha256_of(plan), transport=flaky, alpaca_key=SF.KEY,
                 alpaca_secret=SF.SECRET, sec_user_agent=SF.UA, sleep=lambda s: None)
    man = (tmp_path / "c" / "manifest.json").read_text(encoding="utf-8")
    assert len(calls) == 3 and json.loads(man)["complete"] is False and SF.SECRET not in man and json.loads(man)["network"] is False


def test_B04_the_build_declares_sip_keeps_raw_series_and_issuers_and_never_repairs_a_bar(tmp_path):
    uni = ["AAPL", "ABBV", "ABT", "GOOG", "GOOGL", "SPY"]
    plan, out, tr, bars, sess, _, _ = fetched(tmp_path, split={"AAPL": ("2016-02-16", 4.0)},
                                              tickers=SF.sec_tickers(uni, shared={"GOOGL": "GOOG"}, missing=("ABT",)))
    raw_page = out / "raw" / "bars_all_00_p000.json.gz"                                  # an invalid all-adjusted bar: low <= 0
    body = json.loads(gzip.decompress(raw_page.read_bytes()))
    body["bars"]["ABBV"][10]["l"] = 0.0
    body["bars"]["ABBV"][20]["h"] = round(body["bars"]["ABBV"][20]["c"] * 0.9, 4)                 # high below the close
    body["bars"]["ABBV"][30]["o"] = float("nan")                                          # not a finite price
    nb = json.dumps(body).encode("utf-8")
    raw_page.write_bytes(gzip.compress(nb, mtime=0))
    man = json.loads((out / "manifest.json").read_text(encoding="utf-8"))
    next(r for r in man["responses"] if r["file"] == raw_page.name)["sha256"] = hashlib.sha256(nb).hexdigest()
    (out / "manifest.json").write_text(json.dumps(man), encoding="utf-8")
    p = SB.build(out, kind="SYNTHETIC")
    obj = json.loads(p.read_text(encoding="utf-8"))
    pv = obj["provenance"]
    assert obj["kind"] == "SYNTHETIC" and pv["volume_feed"] == "SIP" and pv["builder"] == RD.BUILDER_VERSION and "SIP feed" in pv["source"]
    assert pv["plan_sha256"] == SB.sha256_of(plan) and pv["raw_manifest_sha256"] == hashlib.sha256((out / "manifest.json").read_bytes()).hexdigest()
    b10, b20 = bars["all"]["ABBV"][10], bars["all"]["ABBV"][20]
    removed = pv["invalid_bars_removed"]
    assert pv["sec_symbols_without_cik"] == ["ABT"] and removed[:2] == [
        {"symbol": "ABBV", "date": sess[10], "ohlc": [b10["o"], b10["h"], 0.0, b10["c"]]},
        {"symbol": "ABBV", "date": sess[20], "ohlc": [b20["o"], round(b20["c"] * 0.9, 4), b20["l"], b20["c"]]}]
    assert [(x["symbol"], x["date"]) for x in removed[2:]] == [("ABBV", sess[30])] and math.isnan(removed[2]["ohlc"][0])
    assert pv["fetch_approval"] == OK_REF
    ds = load(str(p))
    assert ds.sessions == sess and [b.date for b in ds.bars["AAPL"]] == sess and len(ds.bars["ABBV"]) == len(sess) - 3
    assert [b.close for b in ds.bars["AAPL"]] == [b["c"] for b in bars["all"]["AAPL"]]
    s = ds.aux["series"]
    assert s["raw_close"]["AAPL"][sess[0]] == bars["raw"]["AAPL"][0]["c"] and s["raw_volume"]["AAPL"][sess[0]] == 1_500_000
    assert s["split_ratio"]["AAPL"][sess[0]] == pytest.approx(4.0) and s["split_ratio"]["AAPL"][sess[-1]] == pytest.approx(1.0)
    iss = ds.aux["static"]["issuers"]
    assert sorted(next(v["symbols"] for v in iss.values() if "GOOG" in v["symbols"])) == ["GOOG", "GOOGL"]
    from swing_pullback.data import SwingData, SwingDataError
    with pytest.raises(SwingDataError, match="ABT"):                                     # no CIK: the swing baseline refuses (fail closed)
        SwingData(ds)


def test_B05_the_build_refuses_tampered_or_malformed_responses(tmp_path):
    plan, out, *_ = fetched(tmp_path)
    page = out / "raw" / "bars_raw_00_p000.json.gz"
    good = page.read_bytes()
    body = json.loads(gzip.decompress(good))
    body["bars"]["AAPL"][0]["c"] += 1.0
    page.write_bytes(gzip.compress(json.dumps(body).encode(), mtime=0))
    with pytest.raises(SB.BuildError, match="sha256"):
        SB.build(out, kind="SYNTHETIC")
    page.write_bytes(good)

    def rewrite(mutate):
        b = json.loads(gzip.decompress(good))
        mutate(b)
        nb = json.dumps(b).encode()
        page.write_bytes(gzip.compress(nb, mtime=0))
        man = json.loads((out / "manifest.json").read_text(encoding="utf-8"))
        next(r for r in man["responses"] if r["file"] == page.name)["sha256"] = hashlib.sha256(nb).hexdigest()
        (out / "manifest.json").write_text(json.dumps(man), encoding="utf-8")
    rewrite(lambda b: b["bars"]["AAPL"][0].update(t=b["bars"]["AAPL"][0]["t"][:10] + "T13:30:00Z"))
    with pytest.raises(SB.BuildError, match="midnight New York"):
        SB.build(out, kind="SYNTHETIC")
    rewrite(lambda b: b["bars"]["AAPL"].append(b["bars"]["AAPL"][0]))
    with pytest.raises(SB.BuildError, match="duplicate"):
        SB.build(out, kind="SYNTHETIC")
    rewrite(lambda b: b["bars"].update(XYZ=[b["bars"]["AAPL"][0]]))
    with pytest.raises(SB.BuildError, match="unrequested"):
        SB.build(out, kind="SYNTHETIC")
    with pytest.raises(SB.BuildError):
        SB.build(out, kind="REAL")
    page.write_bytes(good)
    man = json.loads((out / "manifest.json").read_text(encoding="utf-8"))
    next(r for r in man["responses"] if r["file"] == page.name)["sha256"] = hashlib.sha256(gzip.decompress(good)).hexdigest()
    man["plan"]["end"] = "2016-12-30"                                                    # the archived plan no longer matches its sha256
    (out / "manifest.json").write_text(json.dumps(man), encoding="utf-8")
    with pytest.raises(SB.BuildError, match="archived plan"):
        SB.build(out, kind="SYNTHETIC")
    _, ok_out, *_ = fetched(tmp_path / "n")
    assert SB.build(ok_out, kind="SYNTHETIC").exists()
    with pytest.raises(SB.BuildError, match="never overwritten"):
        SB.build(ok_out, kind="SYNTHETIC")
    (ok_out / "sip_bundle.json").unlink()
    man = json.loads((ok_out / "manifest.json").read_text(encoding="utf-8"))
    (ok_out / "manifest.json").write_text(json.dumps({**man, "network": True}), encoding="utf-8")
    with pytest.raises(SB.BuildError, match="never SYNTHETIC"):
        SB.build(ok_out, kind="SYNTHETIC")
    assert json.loads(SB.build(ok_out).read_text(encoding="utf-8"))["kind"] == "HISTORICAL"


def test_B07_the_cli_plans_once_and_builds_a_historical_dataset_that_then_needs_approval(tmp_path, capsys):
    p = tmp_path / "plan.json"
    assert SB.main(["plan", "--out", str(p), "--start", "2016-01-04", "--end", "2016-03-31"]) == 0
    plan = json.loads(p.read_text(encoding="utf-8"))
    assert plan == SB.request_plan("2016-01-04", "2016-03-31") and SB.sha256_of(plan) in capsys.readouterr().out
    assert SB.main(["plan", "--out", str(p), "--start", "2016-01-04", "--end", "2016-03-31"]) == 2                 # never overwritten
    _, out, *_ = fetched(tmp_path)
    assert SB.main(["build", "--out", str(out)]) == 0
    built = out / "sip_bundle.json"
    assert json.loads(built.read_text(encoding="utf-8"))["kind"] == "HISTORICAL"                               # the CLI default
    with pytest.raises(Q.ReportRefused):
        Q.report(str(built))


class FakeRequests:
    """Stands in for the requests library: records every call, never touches the network."""
    def __init__(self, status=500):
        self.calls, self.status = [], status

    def get(self, url, **kw):
        self.calls.append((url, kw))
        return types.SimpleNamespace(status_code=self.status, content=b'{"message": "fake"}')


def test_B08_the_http_transport_allows_two_urls_and_never_follows_a_redirect(monkeypatch):
    fake = FakeRequests(status=302)
    monkeypatch.setitem(sys.modules, "requests", fake)
    get = SB.http_transport(timeout=5)
    assert get.network is True
    with pytest.raises(SB.FetchRefused):
        get("https://paper-api.alpaca.markets/v2/orders", {}, {})
    assert fake.calls == []
    assert get(RD.BARS_URL, {"a": 1}, {"h": "x"}) == (302, b'{"message": "fake"}')
    assert fake.calls == [(RD.BARS_URL, {"params": {"a": 1}, "headers": {"h": "x"}, "timeout": 5, "allow_redirects": False})]


def test_B09_credentials_are_read_by_name_and_the_fetch_cli_neither_prints_nor_stores_them(tmp_path, monkeypatch, capsys):
    env = tmp_path / "fake.env"
    env.write_text('ALPACA_API_KEY="FILE-KEY-1"\nALPACA_SECRET_KEY=FILE-SECRET-2\n# SEC_USER_AGENT=commented\n', encoding="utf-8")
    monkeypatch.setenv("ALPACA_API_KEY", "ENV-KEY-3")
    monkeypatch.setenv("SEC_USER_AGENT", "Env Agent env@example.invalid")
    monkeypatch.delenv("ALPACA_SECRET_KEY", raising=False)
    assert SB._credentials(str(env)) == ("FILE-KEY-1", "FILE-SECRET-2", "Env Agent env@example.invalid")   # the explicit file first
    fake = FakeRequests(status=500)
    monkeypatch.setitem(sys.modules, "requests", fake)
    p = tmp_path / "plan.json"
    assert SB.main(["plan", "--out", str(p), "--start", "2016-01-04", "--end", "2016-03-31"]) == 0
    plan_sha = SB.sha256_of(json.loads(p.read_text(encoding="utf-8")))
    args = ["fetch", "--plan", str(p), "--approval", OK_REF, "--env-file", str(env)]
    assert SB.main(args + ["--plan-sha256", "0" * 64, "--out", str(tmp_path / "a")]) == 2 and fake.calls == []
    assert SB.main(args + ["--plan-sha256", plan_sha, "--out", str(tmp_path / "b")]) == 2                     # the fake answers HTTP 500
    assert len(fake.calls) == 1 and fake.calls[0][1]["allow_redirects"] is False and fake.calls[0][0] == RD.SEC_TICKERS_URL
    printed = capsys.readouterr().out
    stored = (tmp_path / "b" / "manifest.json").read_text(encoding="utf-8")
    assert "HTTP 500" in printed and json.loads(stored)["network"] is True
    for secret in ("FILE-KEY-1", "FILE-SECRET-2"):
        assert secret not in printed and secret not in stored


def test_B06_the_iex_builder_global_feed_and_live_paths_are_untouched():
    src = (ROOT / "value_quality" / "ingest.py").read_text(encoding="utf-8")
    assert "feed=DataFeed.IEX" in src and 'DEFAULT_START = "2020-07-27"' in src
    assert 'os.getenv("ALPACA_DATA_FEED", "iex")' in (ROOT / "config.py").read_text(encoding="utf-8")
    for pkg in ("research_data", "swing_eval"):
        for f in (ROOT / pkg).glob("*.py"):
            tree = ast.parse(f.read_text(encoding="utf-8"))
            names = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)} | {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
            mods = {n.module or "" for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)} | {a.name for n in ast.walk(tree) if isinstance(n, ast.Import)
                                                                                             for a in n.names}
            assert "DATA_FEED" not in names and "ALPACA_DATA_FEED" not in names and not {"config", "value_quality.ingest"} & mods, f


# ============================================================================================================ the pre-run report (Q)
@functools.lru_cache(maxsize=None)
def full_universe():
    sess = CAL.sessions("2016-01-04", "2017-12-29")
    n = len(sess)
    specs = [SPF.stock(s, SPF.wave(n, price=40.0 + (i % 9), drift=0.0005, amp=0.03, period=15.0 + i % 7, phase=0.3 * i),
                       cik="ALPHABET" if s in ("GOOG", "GOOGL") else None) for i, s in enumerate(RD.RESEARCH_UNIVERSE)]
    return SPF.dataset(specs, sessions=sess, spy=SPF.wave(n, price=300.0, drift=0.0005, amp=0.01, period=37.0))


def test_Q01_a_complete_synthetic_sip_dataset_is_clean(tmp_path):
    rep = Q.report(str(write(tmp_path, full_universe())))
    assert rep["status"] == "CLEAN" and rep["counts"]["BLOCK"] == rep["counts"]["FINDING"] == 0
    assert rep["report_sha256"] == Q.report_sha256(rep) and rep["dataset"]["volume_feed"] == "SIP"
    goog = checks(rep, "Q07")[0]
    assert goog["kept"] == "GOOG" and goog["earlier_listings_than_kept"] == [] and goog["severity"] == "INFO"
    q5 = checks(rep, "Q05")[0]
    assert checks(rep, "Q04")[0]["severity"] == "INFO" and (q5["possible"], q5["complete"], q5["share"]) == (81 * (503 - 199), 81 * (503 - 199), 1.0)


def _blocked(tmp_path, ds, code, provenance=None):
    rep = Q.report(str(write(tmp_path, ds)), provenance=provenance)
    assert rep["status"] == "BLOCKED" and checks(rep, code, "BLOCK"), [(c["code"], c["severity"], c["title"]) for c in rep["checks"]]
    return rep


def test_Q02_every_blocking_condition_blocks(tmp_path):
    sess = CAL.sessions("2016-01-04", "2017-12-29")
    n = len(sess)
    st = lambda s, **kw: SPF.stock(s, SPF.wave(n, price=45.0, drift=0.0005, amp=0.03, period=17.0), **kw)  # noqa: E731
    ok = dict(sessions=sess)
    _blocked(tmp_path / "a", SPF.dataset([st("AAPL")], feed=None, **ok), "Q02")                                       # no volume feed
    prov = {k: v for k, v in SPF.PROVENANCE.items() if k != "corporate_actions"}
    _blocked(tmp_path / "b", SPF.dataset([st("AAPL")], provenance=prov, **ok), "Q02")                                  # provenance incomplete
    wk = SPF.SESSIONS[:400]
    _blocked(tmp_path / "c", SPF.dataset([SPF.stock("AAPL", SPF.wave(400))], sessions=wk, spy=SPF.wave(400, price=300.0)), "Q04")   # holidays as sessions
    _blocked(tmp_path / "d", SPF.dataset([st("AAPL")], spy_gaps=[100], **ok), "Q04")                                   # a missing benchmark session
    d = SPF.dataset([st("AAPL")], **ok)
    bars = {s: [b.model_dump() for b in rows] for s, rows in d.bars.items()}
    bars["AAPL"].append({**bars["AAPL"][-1], "date": "2018-01-06"})                                                   # a Saturday on a stock
    _blocked(tmp_path / "e", Dataset(bars, kind="SYNTHETIC", source="x", provenance=d.declared, aux=d.aux), "Q04")
    nospy = Dataset({"AAPL": d.bars["AAPL"]}, kind="SYNTHETIC", source="x", provenance=d.declared, aux=d.aux)
    _blocked(tmp_path / "f", nospy, "Q03")
    g = _blocked(tmp_path / "g", SPF.dataset([st("AAPL", raw_gaps=range(n))], **ok), "Q06")                            # no raw close (M-4)
    assert [c["symbols"] for c in checks(g, "Q06", "BLOCK") if c["title"] == "symbols without a raw_close series"] == [["AAPL"]]
    _blocked(tmp_path / "h", SPF.dataset([st("AAPL", volume=None)], **ok), "Q06")                                       # SIP without raw volume
    iex = Q.report(str(write(tmp_path, SPF.dataset([st("AAPL", volume=None)], feed="IEX", **ok), "iex.json")))
    assert not checks(iex, "Q06", "BLOCK") and checks(iex, "Q06", "INFO")                                            # IEX: raw volume never read
    _blocked(tmp_path / "i", SPF.dataset([st("AAPL"), st("ABBV")], issuers={"X": {"symbols": ["AAPL"]}}, **ok), "Q07")
    _blocked(tmp_path / "j", SPF.dataset([st("AAPL")], issuers={"X": {"symbols": ["AAPL"]}, "Y": {"symbols": ["AAPL"]}}, **ok), "Q07")


def test_Q03_every_finding_is_listed_for_the_operator(tmp_path):
    sess = CAL.sessions("2016-01-04", "2017-12-29")
    n = len(sess)
    st = lambda s, **kw: SPF.stock(s, SPF.wave(n, price=45.0, drift=0.0005, amp=0.03, period=17.0), **kw)  # noqa: E731
    rep = Q.report(str(write(tmp_path, SPF.dataset([st("AAPL", gaps=[300, 301]), st("ABBV", raw_gaps=[50])], sessions=sess))))
    hole = checks(rep, "Q05", "FINDING")[0]
    assert hole["by_symbol"] == {"AAPL": [sess[300], sess[301]]} and hole["holes"] == 2 and rep["status"] == "REVIEW_REQUIRED"
    assert checks(rep, "Q06", "FINDING")[0]["by_symbol"] == {"ABBV": [sess[50]]}
    d = SPF.dataset([st("AAPL")], sessions=sess)
    bars = {s: [b.model_dump() for b in rows] for s, rows in d.bars.items()}
    bars["AAPL"][7]["volume"] = 0.0
    aux = copy.deepcopy(d.aux)
    aux["series"]["split_ratio"] = {"AAPL": {x: (0.5 if x < sess[250] else 1.0) for x in sess}}                    # a provider ratio artifact (HON-type)
    rep = Q.report(str(write(tmp_path, Dataset(bars, kind="SYNTHETIC", source="x", provenance=d.declared, aux=aux), "z.json")))
    assert checks(rep, "Q09", "FINDING")[0]["by_symbol"] == {"AAPL": [sess[7]]}
    assert [c["code"] for c in checks(rep, "Q08", "FINDING")] and checks(rep, "Q08", "FINDING")[0]["symbol"] == "AAPL"
    assert checks(rep, "Q08", "INFO")[0]["events"] == [{"symbol": "AAPL", "date": sess[250], "from": 0.5, "to": 1.0}]
    late = SPF.dataset([st("GOOG", cik="A", start=100), st("GOOGL", cik="A")], sessions=sess)                       # M-7: the kept listing starts later
    m7 = checks(Q.report(str(write(tmp_path, late, "m7.json"))), "Q07")[0]
    assert m7["severity"] == "FINDING" and m7["kept"] == "GOOG" and m7["earlier_listings_than_kept"] == ["GOOGL"]


def test_Q04_a_historical_dataset_needs_an_approval_and_the_report_seals_itself(tmp_path, capsys):
    d = SPF.dataset([SPF.stock("AAPL", SPF.wave(503))], sessions=CAL.sessions("2016-01-04", "2017-12-29"), kind="HISTORICAL")
    p = write(tmp_path, d)
    with pytest.raises(Q.ReportRefused):
        Q.report(str(p))
    rep = Q.report(str(p), approval=OK_REF)
    assert rep["dataset"]["kind"] == "HISTORICAL" and rep["report_sha256"] == Q.report_sha256(rep) and rep["approval"] == OK_REF
    assert Q.report_sha256({**rep, "approval": "OPERATOR-APPROVAL:other"}) != rep["report_sha256"]
    assert Q.report_sha256({**rep, "status": "CLEAN"}) != rep["report_sha256"]
    assert Q.main(["--dataset", str(p), "--out", str(tmp_path / "r.json")]) == 2                                     # no approval: refused
    assert Q.main(["--dataset", str(p), "--out", str(tmp_path / "r.json"), "--approval", OK_REF]) == 0
    assert Q.main(["--dataset", str(p), "--out", str(tmp_path / "r.json"), "--approval", OK_REF]) == 2              # never overwritten


def test_Q06_the_requested_window_and_raw_volume_gaps(tmp_path):
    sess = CAL.sessions("2016-01-04", "2017-12-29")
    n = len(sess)
    win = {"start": "2016-01-04T00:00:00Z", "end": "2017-12-30T00:00:00Z"}
    prov = {**SF.SIP_PROVENANCE, "window": win}
    full = SPF.dataset([SPF.stock("AAPL", SPF.wave(n))], sessions=sess, provenance=prov)
    assert not checks(Q.report(str(write(tmp_path, full, "full.json"))), "Q04", "BLOCK")
    inner = sess[3:-3]                                                                   # three sessions missing at each edge of the window
    short = SPF.dataset([SPF.stock("AAPL", SPF.wave(len(inner)))], sessions=inner, provenance=prov)
    rep = Q.report(str(write(tmp_path, short, "short.json")))
    edge = [c for c in checks(rep, "Q04", "BLOCK") if "requested window" in c["title"]]
    assert rep["status"] == "BLOCKED" and edge and edge[0]["missing"] == sess[:3] + sess[-3:] and checks(rep, "Q04", "INFO")
    d = SPF.dataset([SPF.stock("AAPL", SPF.wave(n))], sessions=sess)
    aux = copy.deepcopy(d.aux)
    for k in (5, 6, 7):
        aux["series"]["raw_volume"]["AAPL"].pop(sess[k])
    gappy = Dataset({s: [b.model_dump() for b in rows] for s, rows in d.bars.items()}, kind="SYNTHETIC", source="x", provenance=d.declared, aux=aux)
    rep = Q.report(str(write(tmp_path, gappy, "gappy.json")))
    assert [c["by_symbol"] for c in checks(rep, "Q06", "FINDING") if "raw volume" in c["title"]] == [{"AAPL": sess[5:8]}]


def test_Q05_the_report_computes_no_strategy_rule():
    tree = ast.parse((ROOT / "research_data" / "quality.py").read_text(encoding="utf-8"))
    mods = {n.module or "" for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)} | {a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
    assert not [m for m in mods if m.startswith(("swing_pullback", "swing_eval", "analysis", "momentum", "mean_reversion"))], mods
    assert {m for m in mods if m.startswith("breakout")} == {"breakout.audit"}                                      # the Stage 6.7 data scanner only


# ============================================================================================================ the manifest (M)
def test_M01_a_built_manifest_verifies_and_pins_the_strategy_runs_costs_and_lock(tmp_path):
    dp, rep, mp, rp = package(tmp_path, base()[0])
    m = json.loads(mp.read_text(encoding="utf-8"))
    assert MF.verify(m) == [] and m["manifest_sha256"] == MF.manifest_sha256(m)
    assert m["strategy"]["preregistration_hash"] == SP.preregistration_hash() and m["dataset"]["volume_feed"] == "SIP"
    assert [(r["version"], r["cost"], r["friction"]) for r in m["experiment"]["runs"]] == [(v, c, PL.friction(c)) for v, c in PL.RUNS]
    assert m["thresholds"]["R9"] == SE.R9 and m["fees"]["R12"]["treatment"] == "NOT_MODELLED" and m["fees"]["R13"]["status"] == "ASSUMPTION"
    assert m["pre_run_report"]["report_sha256"] == rep["report_sha256"] and m["dataset"]["file_sha256"] == MF.file_sha256(dp)


TAMPER = [(("experiment", "runs", 0, "friction", "commission_per_order"), 1.0), (("experiment", "runs", 1, "friction", "fee_per_share"), 0.01),
          (("experiment", "runs", 0, "friction", "slippage_bps_per_side"), 7.5), (("experiment", "train_ratio"), 0.6), (("experiment", "sizing", "risk_fraction"), 0.01),
          (("experiment", "arms"), ["RULES", "GATED"]), (("experiment", "execution", "time_exit_fill"), "OPEN"), (("thresholds", "R9", "fold_min_trades"), 10),
          (("thresholds", "R10", "delta_r_min"), 0.0), (("fees", "R12", "fee_per_share"), 0.001), (("fees", "R13", "status"), "VERIFIED"),
          (("strategy", "preregistration_hash"), "0" * 64), (("pre_run_report", "status"), "BLOCKED"), (("pre_run_report", "dataset_sha256"), "f" * 64),
          (("dataset", "volume_feed"), None), (("evidence", "location"), ""), (("dataset", "builder"), None), (("dataset", "volume_feed"), "IEX"),
          (("dataset", "provenance_override"), {"volume_feed": "SIP"}), (("pre_run_report", "provenance_override"), {"universe": "x"}),
          (("pre_run_report", "volume_feed"), "IEX")]


@pytest.mark.parametrize("path,value", TAMPER)
def test_M02_a_tampered_manifest_is_refused_even_when_its_hash_is_recomputed(tmp_path, path, value):
    _, _, mp, _ = package(tmp_path, base()[0])
    m = json.loads(mp.read_text(encoding="utf-8"))
    node = m
    for k in path[:-1]:
        node = node[k]
    node[path[-1]] = value
    assert "manifest_sha256 does not match" in " ".join(MF.verify(m))
    m["manifest_sha256"] = MF.manifest_sha256(m)
    assert MF.verify(m), path


def test_M03_pre_checks_refuse_a_wrong_commit_a_dirty_tree_another_dataset_or_report(tmp_path):
    dp, rep, mp, rp = package(tmp_path, base()[0])
    m = json.loads(mp.read_text(encoding="utf-8"))
    ds = load(str(dp))
    assert MF.pre_checks(m, dataset_path=str(dp), dataset=ds, report=rep, code_commit="c0ffee", tree_clean=True) == []
    assert any("commit" in x for x in MF.pre_checks(m, dataset_path=str(dp), dataset=ds, report=rep, code_commit="beef", tree_clean=True))
    assert any("not clean" in x for x in MF.pre_checks(m, dataset_path=str(dp), dataset=ds, report=rep, code_commit="c0ffee", tree_clean=False))
    other = write(tmp_path, SF.sip_dataset(5)[0], "other.json")
    assert any("sha256" in x for x in MF.pre_checks(m, dataset_path=str(other), dataset=load(str(other)), report=rep, code_commit="c0ffee", tree_clean=True))
    bad = {**rep, "status": "CLEAN"}
    assert any("accepted" in x for x in MF.pre_checks(m, dataset_path=str(dp), dataset=ds, report=bad, code_commit="c0ffee", tree_clean=True))
    for edits, word in (([(("dataset", "expected", "sessions"), 1)], "content"),
                        ([(("dataset", "kind"), "HISTORICAL"), (("pre_run_report", "kind"), "HISTORICAL")], "kind")):
        t = copy.deepcopy(m)
        for key, value in edits:
            node = t
            for k in key[:-1]:
                node = node[k]
            node[key[-1]] = value
        t["manifest_sha256"] = MF.manifest_sha256(t)
        assert MF.verify(t) == [] and any(word in x for x in MF.pre_checks(t, dataset_path=str(dp), dataset=ds, report=rep, code_commit="c0ffee",
                                                                           tree_clean=True)), key


POST = [("friction_model", {"slippage_bps_per_side": 5.0, "commission_per_order": 1.0, "fee_per_share": 0.0}, "friction"),
        ("config_snapshot.sizing", {"equity": 100000.0, "risk_fraction": 0.01, "max_position_pct": 10.0}, "sizing"),
        ("config_snapshot.split.train_ratio", 0.6, "split"), ("arms_run", ["RULES", "GATED"], "arms"),
        ("config_snapshot.adapter.params", {"stop_atr": 2.5}, "adapter"), ("config_snapshot.adapter.max_hold_bars", 12, "adapter"),
        ("execution_assumptions.ambiguous_bar_policy", "TARGET_FIRST", "execution"), ("diagnostics.strategy.costs.preregistered_run", "STRESS_2X", "cost label"),
        ("diagnostics.strategy.preregistration_hash", "0" * 64, "pre-registration"), ("dataset_id", "sha256:other", "dataset id"),
        ("provenance.volume_feed", "IEX", "volume feed"), ("validation_version", "validation_v9", "harness"), ("strategy_id", "OTHER", "strategy id")]


def test_M04_post_checks_catch_every_mismatch_of_a_run_record(tmp_path):
    dp, rep, mp, rp = package(tmp_path, base()[0])
    m = json.loads(mp.read_text(encoding="utf-8"))
    rec = json.loads(PL.run("V1", "BASE", load(str(dp)), created_at=AT).model_dump_json())
    assert MF.post_checks(m, rec, "V1", "BASE") == []
    assert MF.post_checks(m, rec, "V1", "STRESS_2X")                                     # the same record is not the STRESS run
    for dotted, value, word in POST:
        r = copy.deepcopy(rec)
        node = r
        keys = dotted.split(".")
        for k in keys[:-1]:
            node = node[k]
        node[keys[-1]] = value
        probs = MF.post_checks(m, r, "V1", "BASE")
        assert any(word in p for p in probs), (dotted, probs)


def test_M05_the_cli_writes_a_manifest_once_at_the_r17_default_location_with_the_checked_out_commit(tmp_path, capsys):
    from strategy_validation.engine import git_commit_sha
    dp, rep, mp, rp = package(tmp_path, base()[0])
    out = tmp_path / "m_cli.json"
    assert MF.main(["build", "--dataset", str(dp), "--report", str(rp), "--out", str(out), "--created-at", AT]) == 0
    m = json.loads(out.read_text(encoding="utf-8"))
    assert MF.verify(m) == [] and m["manifest_sha256"] in capsys.readouterr().out
    assert m["evidence"]["location"] == str(Path(SE.EVIDENCE_ROOT) / "2026-11-02T000000Z") and m["strategy"]["code_commit"] == git_commit_sha()
    assert MF.main(["build", "--dataset", str(dp), "--report", str(rp), "--out", str(out), "--created-at", AT]) == 2   # never overwritten


# ============================================================================================================ the evaluator (E)
def test_E01_end_to_end_on_synthetic_sip_data_writes_sealed_evidence(tmp_path):
    s, out = run_eval(tmp_path, base()[0])
    assert s["status"] == "COMPLETED" and s["outcomes_accepted"] and s["dataset_kind"] == "SYNTHETIC" and s["statuses"] == ["COMPLETED"]
    assert s["approval_reference"] is None and s["evaluated_at"][:2] == "20" and "NOT ACCEPTED" not in s["comparison"]
    assert {"REGULATORY_FEES_NOT_MODELLED", "REGULATORY_FEE_HISTORY_MISSING", "MODIFIED_D3_BASELINE"} <= set(s["flags"])
    names = sorted(p.relative_to(out).as_posix() for p in out.rglob("*") if p.is_file())
    assert names == ["RESULTS_SUMMARY.md", "comparison.json", "dataset.sha256", "eligibility.json", "environment.json", "evidence.sha256", "manifest.json",
                     "pre_run_report.json", "runs/C1_BASE.json", "runs/C1_STRESS_2X.json", "runs/V1_BASE.json", "runs/V1_STRESS_2X.json",
                     "stability.json", "summary.json", "validation.json"]
    indexed = []
    for line in (out / "evidence.sha256").read_text(encoding="utf-8").splitlines():
        h, rel = line.split("  ", 1)
        assert hashlib.sha256((out / rel).read_bytes()).hexdigest() == h, rel
        indexed.append(rel)
    assert sorted(indexed) == [n for n in names if n != "evidence.sha256"] and len(set(indexed)) == len(indexed)   # every evidence file, once
    fresh = json.loads(PL.run("V1", "BASE", base()[0], created_at=AT).model_dump_json())
    got = json.loads((out / "runs" / "V1_BASE.json").read_text(encoding="utf-8"))
    assert got["candidate_set_hash"] == fresh["candidate_set_hash"] and got["arms"]["RULES"]["metrics"] == fresh["arms"]["RULES"]["metrics"]
    comp = json.loads((out / "comparison.json").read_text(encoding="utf-8"))
    assert comp["outcome"] in ("BEATS_CONTROL", "NO_ADDED_VALUE", "INCONCLUSIVE") and set(comp["delta_R_oos"]) == {"BASE", "STRESS_2X"}
    assert (comp["accepted"], comp["status"], comp["note"]) == (True, "COMPLETED", None)
    assert json.loads((out / "stability.json").read_text(encoding="utf-8"))["accepted"] is True
    assert json.loads((out / "environment.json").read_text(encoding="utf-8"))["evaluated_at"] == s["evaluated_at"]
    assert comp["bootstrap"]["BASE"]["status"] == "REPORTED" and set(comp["reported"]) == {"V1 BASE", "V1 STRESS_2X", "C1 BASE", "C1 STRESS_2X"}
    oos, rm = comp["reported"]["V1 BASE"]["OOS"], fresh["arms"]["RULES"]["metrics"]["OOS"]                   # R10.1 exclusions counted, R10.6 measures
    assert (oos["boundary_purged"], oos["simulation_skipped"]) == (rm["boundary_purged_count"], rm["simulation_skipped_count"])
    assert oos["data_end_exits"] == sum(1 for t in fresh["arms"]["RULES"]["trades"] if t["segment"] == "OOS" and t["exit_reason"] == "DATA_END")
    assert oos["trades"] == comp["trade_counts"]["V1 BASE"]["oos"] and isinstance(oos["friction_heavy"], bool) and oos["exposure"]["trades"] == oos["trades"]
    assert comp["reported"]["V1 BASE"]["train_vs_oos"] == fresh["arms"]["RULES"]["diagnostics"]["train_vs_oos"]
    assert oos["friction_share_of_gross"] == ((rm["friction_total"] / rm["gross_pnl"]) if rm["gross_pnl"] > 0 else None)
    el = json.loads((out / "eligibility.json").read_text(encoding="utf-8"))
    assert [i["item"] for i in el["items"]] == SE.R11 and el["accepted"] is True
    memo = (out / "RESULTS_SUMMARY.md").read_text(encoding="utf-8")
    assert "not a significance test" in memo and "TRAIN observations" in memo and "NO_ADDED_VALUE" in memo and "NOT_ESTABLISHED" in memo


def test_E02_the_evaluator_refuses_before_running(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(EV.PL, "run", lambda *a, **k: calls.append(a))
    dp, rep, mp, rp = package(tmp_path, base()[0])
    ok = dict(commit_fn=lambda: "c0ffee", tree_clean_fn=lambda: True)
    with pytest.raises(EV.EvaluationRefused, match="inside the repository"):
        EV.evaluate(str(mp), str(dp), str(rp), str(ROOT / "evidence_test_78b1"), **ok)
    assert not (ROOT / "evidence_test_78b1").exists()
    (tmp_path / "full").mkdir()
    (tmp_path / "full" / "x").write_text("x")
    with pytest.raises(EV.EvaluationRefused, match="not empty"):
        EV.evaluate(str(mp), str(dp), str(rp), str(tmp_path / "full"), **ok)
    with pytest.raises(EV.EvaluationRefused, match="not the manifest's evidence location"):
        EV.evaluate(str(mp), str(dp), str(rp), str(tmp_path / "elsewhere"), **ok)
    with pytest.raises(EV.EvaluationRefused, match="pre-checks"):
        EV.evaluate(str(mp), str(dp), str(rp), str(tmp_path / "ev"), commit_fn=lambda: "beef", tree_clean_fn=lambda: True)
    with pytest.raises(EV.EvaluationRefused, match="pre-checks"):
        EV.evaluate(str(mp), str(dp), str(rp), str(tmp_path / "ev"), commit_fn=lambda: "c0ffee", tree_clean_fn=lambda: False)
    m = json.loads(mp.read_text(encoding="utf-8"))
    m["experiment"]["sizing"]["risk_fraction"] = 0.01
    mp.write_text(json.dumps(m), encoding="utf-8")
    with pytest.raises(EV.EvaluationRefused, match="does not verify"):
        EV.evaluate(str(mp), str(dp), str(rp), str(tmp_path / "ev"), **ok)
    hist = SF.sip_dataset(6, kind="HISTORICAL")[0]
    hp = tmp_path / "h"
    hp.mkdir()
    dp2 = write(hp, hist)
    rep2 = Q.report(str(dp2), approval=OK_REF)
    m2 = MF.build(dataset_path=str(dp2), report=rep2, code_commit="c0ffee", created_at=AT, evidence_location=str(tmp_path / "o4"))
    (hp / "m.json").write_text(json.dumps(m2), encoding="utf-8")
    (hp / "r.json").write_text(json.dumps(rep2), encoding="utf-8")
    for approval, sha in ((None, None), (OK_REF, None), (OK_REF, "0" * 64), ("approved", m2["manifest_sha256"])):
        with pytest.raises(EV.EvaluationRefused, match="HISTORICAL dataset needs"):
            EV.evaluate(str(hp / "m.json"), str(dp2), str(hp / "r.json"), str(tmp_path / "o4"), approval=approval, approved_manifest_sha256=sha, **ok)
    with pytest.raises(EV.EvaluationRefused, match="directly under"):                   # approved, but not under EVIDENCE_ROOT (R17)
        EV.evaluate(str(hp / "m.json"), str(dp2), str(hp / "r.json"), str(tmp_path / "o4"), approval=OK_REF,
                    approved_manifest_sha256=m2["manifest_sha256"], **ok)
    assert not issubclass(EV.EvaluationRefused, OSError)
    assert calls == []                                                                   # nothing ran


def test_E03_no_socket_and_no_database_while_the_runs_execute(tmp_path, monkeypatch):
    import _socket
    import _sqlite3
    real = PL.run
    entry = lambda: (socket.socket, socket.create_connection, socket.getaddrinfo, _socket.socket, sqlite3.connect, sqlite3.dbapi2.connect,  # noqa: E731
                     _sqlite3.connect)
    orig = entry()
    attempts = (lambda: socket.create_connection(("example.invalid", 80)), lambda: socket.socket(), lambda: socket.getaddrinfo("x", 80),
                lambda: _socket.socket(), lambda: sqlite3.connect(":memory:"), lambda: sqlite3.dbapi2.connect(":memory:"),
                lambda: _sqlite3.connect(":memory:"))
    for attempt in attempts:
        monkeypatch.setattr(EV.PL, "run", lambda *a, _f=attempt, **k: _f())
        with pytest.raises(EV.EvaluationRefused, match="offline"):
            run_eval(tmp_path / str(len(list(tmp_path.iterdir()))), base()[0])
        assert entry() == orig                                                           # restored

    def swallowing(*a, **k):
        try:
            sqlite3.connect(":memory:")
        except Exception:                                                                # noqa: BLE001  a run path that hides the refusal
            pass
        return real(*a, **k)
    monkeypatch.setattr(EV.PL, "run", swallowing)
    with pytest.raises(EV.EvaluationRefused, match="attempt"):
        run_eval(tmp_path / "swallowed", base()[0])
    assert entry() == orig and not (tmp_path / "swallowed" / "ev").exists()


def test_E04_a_run_that_differs_from_the_manifest_is_invalid_and_never_accepted(tmp_path, monkeypatch):
    real = EV.PL.run

    def off(version, cost, dataset, created_at=None):
        if (version, cost) != ("C1", "BASE"):
            return real(version, cost, dataset, created_at=created_at)
        from strategy_validation import engine as E
        return E.run_validation(PL.adapter("C1"), dataset, arms=("RULES",), friction={**PL.friction("BASE"), "commission_per_order": 1.0}, created_at=created_at)
    monkeypatch.setattr(EV.PL, "run", off)
    s, out = run_eval(tmp_path, base()[0])
    assert s["status"] == "INVALID" and not s["outcomes_accepted"] and s["eligible_for_review"] is None
    v = json.loads((out / "validation.json").read_text(encoding="utf-8"))
    assert list(v["invalid_runs"]) == ["C1 BASE"] and any("friction" in p for p in v["invalid_runs"]["C1 BASE"])
    assert json.loads((out / "eligibility.json").read_text(encoding="utf-8"))["accepted"] is False
    comp = json.loads((out / "comparison.json").read_text(encoding="utf-8"))
    assert comp["accepted"] is False and comp["note"] == EV.NOT_ACCEPTED and s["comparison"].startswith("NOT ACCEPTED")
    assert "NOT ACCEPTED (diagnostic only): " in (out / "RESULTS_SUMMARY.md").read_text(encoding="utf-8")


def test_E05_a_non_positive_stop_blocks_acceptance_and_nothing_is_modified(tmp_path):
    ds, _ = SF.wide_range_dataset(3.0, 0.3)
    s, out = run_eval(tmp_path, ds)
    assert s["status"] == "BLOCKED_VALIDATION_FINDING" and not s["outcomes_accepted"] and s["eligible_for_review"] is None
    v = json.loads((out / "validation.json").read_text(encoding="utf-8"))
    f = v["blocking_findings"]
    assert f and {x["code"] for x in f} == {"NON_POSITIVE_STOP"} and {(x["version"], x["cost"]) for x in f} == {("C1", "BASE"), ("C1", "STRESS_2X")}
    assert all(set(x) >= {"symbol", "signal_date", "signal_close", "atr14", "stop", "configuration"} and x["stop"] <= 0 for x in f)
    rec = json.loads((out / "runs" / "C1_BASE.json").read_text(encoding="utf-8"))
    assert sorted(c["invalidation_reference"] for c in rec["candidates"]) == sorted(x["stop"] for x in f if x["cost"] == "BASE")   # kept as produced
    assert json.loads((out / "eligibility.json").read_text(encoding="utf-8"))["accepted"] is False
    assert json.loads((out / "stability.json").read_text(encoding="utf-8"))["note"] == EV.NOT_ACCEPTED and s["stability_V1"].startswith("NOT ACCEPTED")


def test_E09_an_invalid_run_with_a_blocking_finding_reports_both(tmp_path, monkeypatch):
    real = EV.PL.run

    def off(version, cost, dataset, created_at=None):
        if (version, cost) != ("V1", "STRESS_2X"):
            return real(version, cost, dataset, created_at=created_at)
        from strategy_validation import engine as E
        return E.run_validation(PL.adapter("V1"), dataset, arms=("RULES",), friction={**PL.friction("STRESS_2X"), "fee_per_share": 0.01},
                                created_at=created_at)
    monkeypatch.setattr(EV.PL, "run", off)
    s, out = run_eval(tmp_path, SF.wide_range_dataset(3.0, 0.3)[0])
    assert s["statuses"] == ["INVALID", "BLOCKED_VALIDATION_FINDING"] and s["status"] == "INVALID + BLOCKED_VALIDATION_FINDING"
    assert not s["outcomes_accepted"] and s["blocking_findings"] > 0 and list(s["invalid_runs"]) == ["V1 STRESS_2X"]


def test_E10_the_tree_check_reads_the_git_index_without_a_subprocess(tmp_path):
    git = lambda *a: subprocess.run(["git", "-C", str(tmp_path), "-c", "user.name=t", "-c", "user.email=t@example.invalid", "-c", "core.autocrlf=false",  # noqa: E731
                                     *a], check=True, capture_output=True)
    git("init", "-q")
    (tmp_path / ".gitattributes").write_text("* text=auto eol=lf\n", encoding="utf-8")
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "a.py").write_bytes(b"x = 1\n")
    (tmp_path / "pkg" / "b.py").write_bytes(b"y = 2\n")
    (tmp_path / "notes.txt").write_bytes(b"n\n")
    git("add", "-A")
    git("commit", "-q", "-m", "c")
    assert EV.tree_problems(tmp_path) == [] and EV.tree_clean(tmp_path) is True
    (tmp_path / "pkg" / "a.py").write_bytes(b"x = 1\r\n")                                # line ends only: the same code, not a change here
    assert EV.tree_problems(tmp_path) == []
    (tmp_path / "pkg" / "a.py").write_bytes(b"x = 2\n")
    (tmp_path / "notes.txt").unlink()
    (tmp_path / "pkg" / "c.py").write_bytes(b"z = 3\n")
    assert EV.tree_problems(tmp_path) == ["deleted: notes.txt", "modified: pkg/a.py", "untracked: pkg/c.py"] and EV.tree_clean(tmp_path) is False
    plain = tmp_path.parent / (tmp_path.name + "_plain")
    plain.mkdir()
    assert EV.tree_problems(plain)[0].startswith("the git index cannot be read")          # not a repository: never clean
    src = (ROOT / "swing_eval" / "evaluate.py").read_text(encoding="utf-8")
    assert "subprocess" not in src.split('"""', 2)[2].split("def tree_problems")[0] and "import subprocess" not in src


def test_E07_only_a_sip_daily_v1_dataset_is_evaluated_under_its_reports_provenance(tmp_path):
    ok = dict(commit_fn=lambda: "c0ffee", tree_clean_fn=lambda: True)
    dp, rep, mp, rp = package(tmp_path / "a", base()[0], provenance={"volume_feed": "IEX"})          # relabelled feed
    problems = MF.verify(json.loads(mp.read_text(encoding="utf-8")))
    assert any("SIP dataset" in x for x in problems) and any("protected field" in x for x in problems)
    with pytest.raises(EV.EvaluationRefused, match="does not verify"):
        EV.evaluate(str(mp), str(dp), str(rp), str(tmp_path / "a" / "ev"), **ok)
    sess = CAL.sessions("2016-01-04", "2019-12-31")
    iex = SPF.dataset([SPF.stock("AAPL", SPF.wave(len(sess)))], sessions=sess, feed="IEX")        # the Stage 6.4 kind of bundle: no builder
    _, _, mp2, _ = package(tmp_path / "b", iex)
    assert any("SIP dataset" in x for x in MF.verify(json.loads(mp2.read_text(encoding="utf-8"))))
    no_rv = SPF.dataset([SPF.stock("AAPL", SPF.wave(len(sess)), volume=None)], sessions=sess, provenance=dict(SF.SIP_PROVENANCE))
    dp3 = write(tmp_path / "c", no_rv)
    assert Q.report(str(dp3))["status"] == "BLOCKED"                                      # SIP without raw volume (Q06)
    rep3 = Q.report(str(dp3), provenance={"volume_feed": "IEX"})                         # the same file reported as IEX
    assert rep3["status"] != "BLOCKED"
    m3 = MF.build(dataset_path=str(dp3), report=rep3, code_commit="c0ffee", created_at=AT, evidence_location=str(tmp_path / "c" / "ev"))
    assert any("another provenance" in x for x in MF.verify(m3))
    m3["pre_run_report"].update(provenance_override={}, volume_feed="SIP")                # the recorded fields forged, the hash recomputed
    m3["manifest_sha256"] = MF.manifest_sha256(m3)
    assert MF.verify(m3) == []
    assert any("report file was made under another provenance" in x for x in MF.pre_checks(m3, dataset_path=str(dp3), dataset=load(str(dp3)),
                                                                                            report=rep3, code_commit="c0ffee", tree_clean=True))


def test_E08_the_cli_refuses_a_manifest_of_another_commit_before_any_run(tmp_path, monkeypatch, capsys):
    calls = []
    monkeypatch.setattr(EV.PL, "run", lambda *a, **k: calls.append(a))
    dp, rep, mp, rp = package(tmp_path, base()[0])                                       # code_commit "c0ffee": not the checked-out commit
    assert EV.main(["--manifest", str(mp), "--dataset", str(dp), "--report", str(rp), "--out", str(tmp_path / "ev")]) == 2
    assert "REFUSED" in capsys.readouterr().out and calls == [] and not (tmp_path / "ev").exists()


def test_E06_a_non_positive_stop_in_a_train_view_neighbour_alone_blocks_too(tmp_path):
    ds, _ = SF.wide_range_dataset(1.25, 0.80, dips=(230, 260, 290, 320, 400, 450))
    s, out = run_eval(tmp_path, ds)
    f = json.loads((out / "validation.json").read_text(encoding="utf-8"))["blocking_findings"]
    assert s["status"] == "BLOCKED_VALIDATION_FINDING" and f
    assert {(x["version"], json.dumps(x["configuration"]), x["cost"]) for x in f} == {("V1", '{"stop_atr": 2.5}', "TRAIN view")}
    v1 = json.loads((out / "runs" / "V1_BASE.json").read_text(encoding="utf-8"))
    assert all(x["signal_date"] <= v1["train_end"] for x in f) and len(f) == 4                        # never an OOS signal
    assert [c["signal_date"] for c in v1["candidates"] if c["signal_date"] > v1["train_end"]]           # though V1 has OOS candidates
    for name in ("V1_BASE", "C1_BASE"):
        assert all(c["invalidation_reference"] > 0 for c in json.loads((out / "runs" / f"{name}.json").read_text(encoding="utf-8"))["candidates"])


# ============================================================================================================ the analysis (N)
SESS = CAL.sessions("2016-01-04", "2019-12-31")


def fake_rec(per_fold, *, oos_from=None, n_oos=None):
    """A record whose RULES trades put per_fold[k] = (count, r_net, net_pnl) into fold k (signal dates spread inside it)."""
    bounds = __import__("swing_pullback.diagnostics", fromlist=["fold_bounds"]).fold_bounds(SESS)
    oos_start = oos_from or SESS[int(len(SESS) * 0.7)]
    tr = []
    for b, (count, r, pnl) in zip(bounds, per_fold):
        span = SESS[SESS.index(b["start"]):SESS.index(b["end"]) + 1]
        for k in range(count):
            d = span[(k * 3) % len(span)]
            i = SESS.index(d)
            tr.append({"signal_date": d, "entry_date": SESS[i + 1], "exit_date": SESS[min(i + 5, len(SESS) - 1)], "segment": "OOS" if d >= oos_start else "TRAIN",
                       "r_net": r, "net_pnl": pnl, "holding_bars": 5, "exit_reason": "TIME_EXIT", "size_status": "CAPPED"})
    return {"arms": {"RULES": {"trades": tr, "metrics": {}}}, "oos_start": oos_start, "oos_end": SESS[-1], "oos_sessions": len(SESS) - SESS.index(oos_start),
            "train_start": SESS[0], "train_end": SESS[SESS.index(oos_start) - 1], "candidates": [], "verdict": {"verdict": "PASS"}}


def test_N01_r9_stability_outcomes():
    assert AN.stability(fake_rec([(25, 0.2, 10.0)] * 6), SESS)["outcome"] == "STABLE"
    assert AN.stability(fake_rec([(25, 0.2, 10.0)] * 4 + [(10, 0.2, 10.0)] * 2), SESS)["outcome"] == "INSUFFICIENT"     # 4 evaluable < 5
    assert AN.stability(fake_rec([(25, 0.2, 10.0)] * 3 + [(25, -0.2, -10.0)] * 3), SESS)["outcome"] == "UNSTABLE"   # 3 of 6 positive < 4
    s = AN.stability(fake_rec([(25, 0.2, 10.0)] * 4 + [(25, -0.2, -10.0)] * 2), SESS)
    assert s["outcome"] == "STABLE" and (s["positive_folds"], s["positive_needed"]) == (4, 4)
    assert AN.stability(fake_rec([(25, 0.04, 10.0)] * 6), SESS)["outcome"] == "UNSTABLE"                            # median fold mean R <= 0.05
    dom = AN.stability(fake_rec([(25, 0.2, 1.0)] * 5 + [(25, 2.0, 100.0)]), SESS)
    assert dom["outcome"] == "UNSTABLE" and dom["dominant_folds"] == [6]                                             # one fold >= 80 % of net
    assert AN.stability(fake_rec([(20, 0.2, 10.0)] * 6), SESS)["evaluable"] == 6 and AN.stability(fake_rec([(19, 0.2, 10.0)] * 6), SESS)["evaluable"] == 0
    zero = AN.stability(fake_rec([(25, 0.2, 10.0)] * 3 + [(25, 0.0, 0.0)] * 3), SESS)
    assert (zero["positive_folds"], zero["outcome"]) == (3, "UNSTABLE")                                              # net P/L > 0, strictly
    five = AN.stability(fake_rec([(25, 0.2, 10.0)] * 3 + [(25, -0.2, -10.0)] * 2 + [(10, 0.2, 10.0)]), SESS)
    assert (five["evaluable"], five["positive_folds"], five["positive_needed"], five["outcome"]) == (5, 3, 4, "UNSTABLE")     # ceil(2/3 x 5) = 4
    tie = AN.stability(fake_rec([(25, 0.05, 10.0)] * 6), SESS)
    assert tie["median_fold_mean_R"] == 0.05 and tie["outcome"] == "UNSTABLE"                                         # strictly above 0.05
    loss = AN.stability(fake_rec([(25, 0.5, 1.0)] * 4 + [(25, 0.5, -10.0)] * 2), SESS)                             # 4 of 6 positive, total < 0
    assert (loss["positive_folds"], loss["total_net_pnl"], loss["outcome"]) == (4, -400.0, "UNSTABLE") and loss["concentration"].startswith("NOT_SATISFIABLE")
    edge = AN.stability(fake_rec([(25, 0.2, 1.0)] * 5 + [(25, 0.2, 20.0)]), SESS)                                    # 500 / 625 = exactly 80 %
    assert edge["dominant_folds"] == [6] and edge["outcome"] == "UNSTABLE"


def _four(v1_r, c1_r, v1_stress=None, c1_stress=None, n=25):
    return {("V1", "BASE"): fake_rec([(n, v1_r, v1_r)] * 6), ("V1", "STRESS_2X"): fake_rec([(n, v1_stress or v1_r, 1.0)] * 6),
            ("C1", "BASE"): fake_rec([(n, c1_r, c1_r)] * 6), ("C1", "STRESS_2X"): fake_rec([(n, c1_stress or c1_r, 1.0)] * 6)}


def test_N02_r10_outcomes_follow_the_confirmed_wording():
    assert AN.comparison(_four(0.30, 0.20), SESS)["outcome"] == "BEATS_CONTROL"
    assert AN.comparison(_four(0.30, 0.20, v1_stress=0.10, c1_stress=0.15), SESS)["outcome"] == "NO_ADDED_VALUE"   # Δ <= 0 in one cost run
    assert AN.comparison(_four(0.23, 0.20), SESS)["outcome"] == "INCONCLUSIVE"                                     # 0 < Δ < 0.05
    c = AN.comparison(_four(0.30, 0.20, v1_stress=0.23, c1_stress=0.20), SESS)                                       # C-a needs BOTH cost runs
    assert (c["outcome"], c["C_a"], c["C_b"]) == ("INCONCLUSIVE", False, True)
    assert AN.comparison(_four(0.05, 0.0), SESS)["delta_R_oos"]["BASE"] == 0.05
    assert AN.comparison(_four(0.05, 0.0), SESS)["outcome"] == "BEATS_CONTROL"                                      # Δ = 0.05 exactly: >= 0.05
    c = AN.comparison(_four(0.30, 0.20, n=4), SESS)
    assert c["outcome"] == "INCONCLUSIVE" and "insufficient trades" in c["why"]                                     # never NO_ADDED_VALUE on counts
    few = _four(0.30, 0.20)
    few[("C1", "BASE")] = fake_rec([(25, 0.20, 0.2)] * 3 + [(19, 0.20, 0.2)] * 3)                                  # enough trades, 3 paired folds
    c = AN.comparison(few, SESS)
    assert (c["outcome"], c["C_a"], c["C_b"], len(c["paired_folds"])) == ("INCONCLUSIVE", True, False, 3) and "paired folds" in c["why"]
    late = _four(0.30, 0.20)
    late[("V1", "BASE")] = fake_rec([(25, 0.10, 1.0)] * 3 + [(25, 0.30, 1.0)] * 3)                                  # behind in TRAIN, ahead in OOS
    c = AN.comparison(late, SESS)
    assert (c["outcome"], c["C_a"], c["C_b"], c["v1_ahead"]) == ("INCONCLUSIVE", True, False, 3) and c["why"] == "C-a or C-b not met"
    lag = _four(0.30, 0.20)
    lag[("V1", "BASE")] = fake_rec([(25, 0.30, 1.0)] * 3 + [(25, 0.10, 1.0)] * 3)                                   # V1 ahead in 3 of 6 folds only
    c = AN.comparison(lag, SESS)
    assert c["C_b"] is False and c["outcome"] == "NO_ADDED_VALUE"                                                  # its OOS folds trail
    assert any("not a significance test" in s for s in c["interpretation"])
    oos_only = _four(0.30, 0.20)
    oos_only[("V1", "BASE")] = fake_rec([(0, 0, 0)] * 5 + [(25, 0.30, 1.0)])                                        # 25 OOS, 25 in total < 30
    train_heavy = _four(0.30, 0.20)
    train_heavy[("V1", "BASE")] = fake_rec([(25, 0.30, 1.0)] * 4 + [(0, 0, 0), (10, 0.30, 1.0)])                   # 110 in total, 10 OOS < 20
    for recs in (oos_only, train_heavy):
        c = AN.comparison(recs, SESS)
        assert c["outcome"] == "INCONCLUSIVE" and "insufficient trades" in c["why"] and "V1 BASE" in c["why"]
    even = _four(0.30, 0.20)
    even[("V1", "BASE")] = fake_rec([(25, 0.30, 1.0)] * 3 + [(25, 0.20, 1.0)] * 3)                                  # level in 3 folds: not ahead
    c = AN.comparison(even, SESS)
    assert (c["v1_ahead"], c["C_b"]) == (3, False)


def test_N03_the_bootstrap_is_deterministic_joint_by_month_and_descriptive():
    recs = _four(0.30, 0.20)
    a = AN.bootstrap(AN.trades(recs[("V1", "BASE")], "OOS"), AN.trades(recs[("C1", "BASE")], "OOS"))
    b = AN.bootstrap(AN.trades(recs[("V1", "BASE")], "OOS"), AN.trades(recs[("C1", "BASE")], "OOS"))
    assert a == b and a["status"] == "REPORTED" and a["role"] == "descriptive" and a["resamples_used"] + a["skipped"] == 2000
    assert a["percentiles"]["50"] == pytest.approx(0.10) and a["share_delta_le_0"] == 0.0
    assert AN.bootstrap([], [])["status"] == "NO_TRADES"
    months = sorted({d[:7] for d in SESS})[:24]
    tr = lambda shift: [{"signal_date": f"{m}-15", "r_net": k * 0.1 + shift} for k, m in enumerate(months) for _ in range(2)]  # noqa: E731
    j = AN.bootstrap(tr(0.05), tr(0.0))                                                  # the same months for both: Δ is 0.05 in every resample
    assert j["months"] == 24 and j["skipped"] == 0 and j["percentiles"]["5"] == pytest.approx(0.05) and j["percentiles"]["95"] == pytest.approx(0.05)
    v = AN.bootstrap(tr(0.0), [{"signal_date": f"{m}-15", "r_net": 0.0} for m in months])
    assert v == AN.bootstrap(tr(0.0), [{"signal_date": f"{m}-15", "r_net": 0.0} for m in months])          # seeded: reproducible
    assert v["percentiles"]["5"] < v["percentiles"]["50"] < v["percentiles"]["95"]


def test_N04_exposure_counts_position_bars_and_concurrent_open_trades():
    rec = fake_rec([(0, 0, 0)] * 5 + [(2, 0.1, 1.0)])
    rec["arms"]["RULES"]["trades"][1]["exit_reason"] = "DATA_END"
    m = AN.measures(rec, SESS)["OOS"]
    assert (m["data_end_exits"], m["by_exit"]) == (1, {"DATA_END": 1, "TIME_EXIT": 1})
    e = AN.exposure(rec, SESS, "OOS")
    tr = AN.trades(rec, "OOS")
    assert e["trades"] == len(tr) == 2 and e["position_bars"] == 10 and e["net_pnl_per_1000_position_bars"] == pytest.approx(200.0)
    window = [d for d in SESS if rec["oos_start"] <= d <= rec["oos_end"]]
    assert e["mean_concurrent_open_trades"] == pytest.approx(sum(sum(1 for t in tr if t["entry_date"] <= d <= t["exit_date"]) for d in window) / len(window))


def test_N05_r11_needs_all_six_items():
    recs = _four(0.30, 0.20)
    recs[("V1", "STRESS_2X")]["arms"]["RULES"]["metrics"] = {"OOS": {"expectancy_R": 0.2}}
    recs[("V1", "BASE")]["diagnostics"] = {"strategy": {"sensitivity": {"label": "ROBUST"}}}
    stab, comp = {"outcome": "STABLE"}, {"outcome": "BEATS_CONTROL"}
    ok = AN.eligibility(recs, stab, comp, {"status": "CLEAN"}, [], [])
    assert ok["eligible_for_review"] is True and [i["item"] for i in ok["items"]] == SE.R11
    assert AN.eligibility(recs, stab, comp, {"status": "CLEAN"}, [{"code": "NON_POSITIVE_STOP"}], [])["eligible_for_review"] is False
    assert AN.eligibility(recs, stab, {"outcome": "INCONCLUSIVE"}, {"status": "CLEAN"}, [], [])["eligible_for_review"] is False
    assert AN.eligibility(recs, {"outcome": "INSUFFICIENT"}, comp, {"status": "CLEAN"}, [], [])["eligible_for_review"] is False
    assert AN.eligibility(recs, stab, comp, {"status": "BLOCKED"}, [], [])["eligible_for_review"] is False
    assert AN.eligibility(recs, stab, comp, {"status": "REVIEW_REQUIRED"}, [], ["C1 BASE"], findings_accepted_by="x")["eligible_for_review"] is False
    assert AN.eligibility(recs, stab, comp, {"status": "REVIEW_REQUIRED"}, [], [])["eligible_for_review"] is False       # no acceptance recorded
    acc = AN.eligibility(recs, stab, comp, {"status": "REVIEW_REQUIRED"}, [], [], findings_accepted_by="OPERATOR-APPROVAL:r of manifest m")
    assert acc["eligible_for_review"] is True and acc["items"][5]["evidence"]["findings_accepted_by"] == "OPERATOR-APPROVAL:r of manifest m"
    for mutate in (lambda r: r[("V1", "BASE")].update(verdict={"verdict": "FAIL"}),
                   lambda r: r[("V1", "STRESS_2X")]["arms"]["RULES"].update(metrics={"OOS": {"expectancy_R": 0.0}}),
                   lambda r: r[("V1", "BASE")].update(diagnostics={"strategy": {"sensitivity": {"label": "SENSITIVE"}}})):
        r = copy.deepcopy(recs)
        mutate(r)
        e = AN.eligibility(r, stab, comp, {"status": "CLEAN"}, [], [])
        assert e["eligible_for_review"] is False and sum(1 for i in e["items"] if not i["ok"]) == 1


def test_N06_stop_and_integrity_findings():
    from types import SimpleNamespace
    rec = fake_rec([(0, 0, 0)] * 6)
    rec.update(candidates=[{"symbol": "AAPL", "signal_date": "2016-05-02", "invalidation_reference": s, "features": {"close": 10.0, "atr": 5.0}}
                           for s in (0.0, 0.01, -3.0, None)],
               causality={"critical": False}, audit={"status": "PASS"}, integrity={"same_candidate_set": True})
    raw = lambda s: SimpleNamespace(symbol="ABBV", signal_date="2016-06-01", invalidation_reference=s, features={"close": 8.0, "atr": 4.0})  # noqa: E731
    f = AN.stop_findings({("V1", "BASE"): rec}, [({"stop_atr": 2.5}, raw(0.0)), ({"stop_atr": 1.5}, raw(2.0))])
    assert [(x["configuration"], x["cost"], x["stop"]) for x in f] == [("base", "BASE", 0.0), ("base", "BASE", -3.0), ({"stop_atr": 2.5}, "TRAIN view", 0.0)]
    assert f[0] == {"code": "NON_POSITIVE_STOP", "version": "V1", "configuration": "base", "cost": "BASE", "symbol": "AAPL", "signal_date": "2016-05-02",
                         "signal_close": 10.0, "atr14": 5.0, "stop": 0.0}
    assert AN.integrity_findings({("V1", "BASE"): rec}) == []
    bad = {**rec, "causality": {"critical": True}, "audit": {"status": "FAIL"}, "integrity": {"same_candidate_set": False}}
    assert [x["code"] for x in AN.integrity_findings({("C1", "STRESS_2X"): bad})] == ["CAUSALITY_CRITICAL", "AUDIT_FAILED", "CANDIDATE_SET_MISMATCH"]


# ============================================================================================================ isolation (X)
FORBIDDEN = ("execution", "safety", "paper", "proposals", "api", "trader_view", "robinhood", "rh_gateway", "unified_portfolio", "market_intel", "market_scan",
             "alpaca", "anthropic", "strategy_validation.store", "strategy_validation.service", "strategy_validation.cli", "strategy_registry", "database")


def test_X01_the_new_packages_import_no_broker_execution_store_or_live_module():
    for pkg in ("research_data", "swing_eval"):
        for f in sorted((ROOT / pkg).glob("*.py")):
            tree = ast.parse(f.read_text(encoding="utf-8"))
            mods = {n.module or "" for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)} | {a.name for n in ast.walk(tree) if isinstance(n, ast.Import)
                                                                                             for a in n.names}
            assert not [m for m in mods if m == "config" or m.startswith(FORBIDDEN)], (f.name, mods)
            top = {a.name for n in tree.body if isinstance(n, ast.Import) for a in n.names} | {n.module for n in tree.body if isinstance(n, ast.ImportFrom)}
            assert not top & {"requests", "urllib", "urllib.request", "http.client"}, f.name             # the network library: only inside http_transport
    src = (ROOT / "research_data" / "sip_bundle.py").read_text(encoding="utf-8")
    assert src.count("import requests") == 1 and "def http_transport" in src and "requests.post" not in src and ".delete(" not in src


def test_X02_nothing_outside_the_new_packages_and_tests_imports_them():
    for p in ROOT.rglob("*.py"):
        rel = p.relative_to(ROOT).as_posix()
        if rel.startswith(("research_data/", "swing_eval/", "tests/", ".venv/", "scratchpad/", ".claude/")) or "/site-packages/" in rel:
            continue
        t = p.read_text(encoding="utf-8", errors="replace")
        assert "research_data" not in t and "swing_eval" not in t, rel


# ============================================================================================================ the documents (G)
def test_G01_the_documents_state_the_lock_the_gates_and_the_limits():
    doc = (ROOT / "DESIGN_78B1_SIP_EVALUATION.md").read_text(encoding="utf-8")
    for s in ("STAGE_7_8B1_DECISION_LOCK.md", "OPERATOR-APPROVAL:", "volume_feed", "2016-01-04", "SIP", "IEX builder", "byte-identical", "manifest",
              "pre-run", "BLOCKED_VALIDATION_FINDING", "non-positive stop", "not a significance test", "TRAIN observations", "NO_ADDED_VALUE",
              "$50", "not a verified bound", "REGULATORY_FEE_HISTORY_MISSING", "evidence", "outside the repository", "synthetic", "No SIP history was fetched"):
        assert s in doc, s
    d78 = (ROOT / "DESIGN_78_SWING_PULLBACK.md").read_text(encoding="utf-8")
    assert "the close of the 10th bar of the symbol in the trade" in d78 and "its symbol's next `max_hold_bars` bars" in d78



# ============================================================================================================ scope (F)
def _git(*a):
    return subprocess.run(["git", *a], cwd=ROOT, capture_output=True, text=True).stdout


def at_base(rel):
    run = subprocess.run(["git", "show", f"{FL.BASE_78B1}:{rel}"], cwd=ROOT, capture_output=True)
    assert run.returncode == 0, f"{rel} is not in {FL.BASE_78B1} (fetch-depth 0 is required)"
    return run.stdout.decode("utf-8").replace("\r\n", "\n")


def now_text(rel):
    return (ROOT / rel).read_text(encoding="utf-8").replace("\r\n", "\n")


NEW_FILES_78B1 = {"DESIGN_78B1_SIP_EVALUATION.md", "tests/test_sip_eval_78b1.py", "tests/sip_eval_fixtures.py"} | {
    f"research_data/{m}" for m in ("__init__.py", "nyse_calendar.py", "quality.py", "sip_bundle.py")} | {
    f"swing_eval/{m}" for m in ("__init__.py", "analysis.py", "evaluate.py", "manifest.py")}
APP_EDITS_78B1 = {".github/ci/pytest_groups.json", "DESIGN_78_SWING_PULLBACK.md"}                 # the CI entry; the M-6 wording (pinned in REPLAY_78B1)
FROZEN_DIRS_78B1 = ("setups", "ppe", "evidence", "evidence_refresh", "execution", "proposals", "safety", "autorun", "decision_flow", "dynamic_universe",
                    "unified_portfolio", "trader_view", "scripts", "forward_validation", "account_refresh", "rh_gateway", "frontend", "browser_tests", "api",
                    "database", "data", "value_quality", "market_intel", "market_scan", "scanner", "strategy_registry", "strategy_validation", "analysis",
                    "momentum", "mean_reversion", "breakout", "rotation_walkforward", "backtest", "paper", "swing_pullback")


def test_F01_the_change_set_is_exactly_the_declared_files_and_no_existing_module_changed():
    assert FL.BASE_78B1 == "b52e0dc7d91672a2db7dd95f6f19f3e1c25d138b" and subprocess.run(["git", "cat-file", "-e", f"{FL.BASE_78B1}^{{commit}}"],
                                                                                          cwd=ROOT).returncode == 0
    changed = set(_git("diff", "--name-only", FL.BASE_78B1).split()) | set(_git("ls-files", "--others", "--exclude-standard").split())
    changed = {p for p in changed if not p.startswith(("scratchpad/", "runtime/", "data/market_intel", "logs/", "browser_tests/artifacts/")) and (ROOT / p).exists()}
    assert changed == FL.NEW_78B1 | FL.EDITED_78B1, {"unexpected": sorted(changed - FL.NEW_78B1 - FL.EDITED_78B1),
                                                     "missing": sorted((FL.NEW_78B1 | FL.EDITED_78B1) - changed)}
    in_base = set(_git("ls-tree", "-r", "--name-only", FL.BASE_78B1).split())
    assert {p for p in changed if p not in in_base} == FL.NEW_78B1 and {p for p in changed if p in in_base} == FL.EDITED_78B1
    assert FL.NEW_78B1 == NEW_FILES_78B1
    assert {p for p in FL.EDITED_78B1 if not p.startswith("tests/")} == APP_EDITS_78B1
    assert FL.SEMANTIC_78B1 == {} and FL.INSTRUMENTATION_78B1 == {}
    for d in FROZEN_DIRS_78B1:                                                                 # every existing module: the IEX builder, the harness, swing_pullback/
        for p in sorted((ROOT / d).rglob("*")):
            rel = p.relative_to(ROOT).as_posix()
            if p.is_file() and rel in in_base and "__pycache__" not in rel:
                assert now_text(rel) == at_base(rel), rel
    for rel in ("config.py", "api/server.py", "autorun/calendar.py", "autorun/session.py", ".gitignore", "requirements.txt", "requirements-dev.txt",
                "watchlist.txt", "STRATEGY_STANDARD_V1.md", "STRATEGY_VALIDATION_HARNESS.md", "main.py", ".github/workflows/tests.yml"):
        assert now_text(rel) == at_base(rel), rel                                          # no config key, route, lifespan hook, dependency or workflow edit
    for rel in sorted(FL.NEW_78B1 | FL.EDITED_78B1):
        if rel.endswith((".py", ".js", ".html", ".md")):
            m61, m54 = "# >>> " + "Stage 6.1", "# >>> " + "Stage 5.4"                    # built, so this file never carries the markers itself
            assert m61 not in now_text(rel) and m54 not in now_text(rel), rel


def test_F02_the_frozen_hashes_and_settings_are_unchanged_and_nothing_enables_a_strategy():
    out = subprocess.run([sys.executable, "-c", "import config, forward_validation as FV; from execution import exposure_gate as G; "
                          "print(FV.frozen_hash()); print(G.contract_hash(G.from_config(config))); print(G.contract_hash(G.GateSettings(enabled=True)))"],
                         cwd=ROOT, capture_output=True, text=True)
    assert out.stdout.split() == ["b5d4a56ef5bba61f8250b3cd25182cc1655738adf0605a77671fd029883f8530",
                                  "2552bd6639f691b828624250665519cc90c4a00da7f70893dbb635f7551006fa",
                                  "d2819342f6c8ccb1cd4a0ce2bfb5d1b0664f2453770c3e6736f7be420a17aed4"], out.stderr[-600:]
    import config
    import execution as X
    from dynamic_universe import plan as DUP
    from market_intel.settings import settings as mi_settings
    from strategy_registry import registry as RG
    from strategy_validation import adapters as AD
    assert X.MODES == ("MONITOR_ONLY", "PAPER_AUTO") and mi_settings()["mode"] == "OFF" and config.EVIDENCE_REFRESH_MODE == "OFF"
    assert DUP.settings()["mode"] == "OFF"
    assert not [n for n in dir(config) if "SIP_" in n or "SWING_TREND" in n or "SWING_PULLBACK" in n or "RESEARCH_DATA" in n or "SWING_EVAL" in n]   # no new key
    assert set(AD.ADAPTERS) == {"DOWNSIDE_DISLOCATION_RECOVERY", "VALUE_QUALITY", "MOMENTUM", "MEAN_REVERSION", "BREAKOUT"}   # nothing registered
    assert not [s for s in RG.by_id() if "SWING" in s]


def test_F03_every_bookkeeping_edit_is_pinned_exactly_and_the_ledger_changes_only_the_declared_entries():
    for rel, edits in REPLAY_78B1.items():
        text = now_text(rel)
        for old, new in edits:
            assert text.count(new) == 1, (rel, new[:100])
            text = text.replace(new, old)
        assert text == at_base(rel), rel
    assert set(REPLAY_78B1) == FL.EDITED_78B1 - set(FL.LEDGER) - {"tests/freeze_ledger.py"}   # every other edited file carries a ledger entry
    fl, base = now_text("tests/freeze_ledger.py"), at_base("tests/freeze_ledger.py")
    start = fl.index("\nStage 7.8B-1 (the isolated SIP")
    para = fl[start:fl.index('"""', start)]
    rebuilt = fl.replace(para, "", 1).replace(" + NEW_75 + NEW_78 + NEW_78B1 plus this ledger.", " + NEW_75 + NEW_78 plus this ledger.", 1)
    decl = rebuilt[rebuilt.index("BASE_78B1 = "):rebuilt.index("\n", rebuilt.index("INSTRUMENTATION_78B1: dict = {}")) + 1]
    rebuilt = rebuilt.replace(decl, "", 1)
    led = lambda t: t[t.index("LEDGER = {"):t.index("}\n", t.index("LEDGER = {")) + 2]  # noqa: E731
    rebuilt = rebuilt.replace(led(rebuilt), led(base), 1)
    assert rebuilt == base                                                                  # the declarations, the paragraph, one line and the ledger
    old = ast.literal_eval(led(base)[len("LEDGER = "):])
    assert set(FL.LEDGER) == set(old)                                                       # no new ledger key
    assert {k for k in old if old[k] != FL.LEDGER[k]} == {".github/ci/pytest_groups.json", "tests/test_strategy_validation_62.py"} == FL.EDITED_78B1 & set(old)
    for rel in FL.LEDGER:
        assert FL.view(rel, FL.now_text(rel)) == FL.base_text(rel), rel


REPLAY_78B1 = {
    'DESIGN_78_SWING_PULLBACK.md': [
        ('| 8 | Cooldown | after a candidate at session t′, no candidate for the symbol at t′+1 … t′+9 (dataset sessions; signal dates only; a blocked session never restarts it); 9 sessions in every sensitivity neighbour |\n| 9 | Entry | the open of t+1 (`NEXT_BAR_OPEN`); an entry opening at or below the stop, or at or above the target, is skipped (the harness) |\n| 10 | Stop / target | signal close − 2 × ATR(14) / signal close + 3 × ATR(14), Wilder ATR, fixed at the signal |\n',
         "| 8 | Cooldown | after a candidate at session t′, no candidate for the symbol at t′+1 … t′+9 (dataset sessions; signal dates only; a blocked session never restarts it); 9 sessions in every sensitivity neighbour |\n| 9 | Entry | the open of the symbol's next bar after t (`NEXT_BAR_OPEN`; t+1 unless the symbol has no bar there); an entry opening at or below the stop, or at or above the target, is skipped (the harness) |\n| 10 | Stop / target | signal close − 2 × ATR(14) / signal close + 3 × ATR(14), Wilder ATR, fixed at the signal |\n"),
        ("| 10 | Stop / target | signal close − 2 × ATR(14) / signal close + 3 × ATR(14), Wilder ATR, fixed at the signal |\n| 11 | Time exit | the close of the 10th session in the trade (the entry session is the first): `max_hold_bars` = 10, the adapter's, never a candidate's |\n| 12 | Exits | the harness only: gap through the stop at the open, stop, target, stop-first on an ambiguous bar, time, data end |\n",
         "| 10 | Stop / target | signal close − 2 × ATR(14) / signal close + 3 × ATR(14), Wilder ATR, fixed at the signal |\n| 11 | Time exit | the close of the 10th bar of the symbol in the trade (the entry bar is the first; a session without a bar does not count): `max_hold_bars` = 10, the adapter's, never a candidate's |\n| 12 | Exits | the harness only: gap through the stop at the open, stop, target, stop-first on an ambiguous bar, time, data end |\n"),
        ('\nThe indicators are written in plain Python (`swing_pullback/indicators.py`). They reproduce the `analysis/indicators.py` formulas: the RSI\n',
         "\nRules 9 and 11 count the symbol's own bars, as the harness does, not dataset sessions: a missing bar moves the entry or the time exit to a later\nsession. (Wording corrected in Stage 7.8B-1, final-review M-6; the behaviour is unchanged, and the `swing_pullback/` docstrings are left as they\nare so that the package stays byte-identical.)\n\nThe indicators are written in plain Python (`swing_pullback/indicators.py`). They reproduce the `analysis/indicators.py` formulas: the RSI\n"),
        ('average loss is 0. They are causal by construction: a prefix gives the same values. `analysis/` itself is not imported. The SMA is the correctly rounded mean\nof each window (`math.fsum` / n). The three strict comparisons with a mean are decided exactly, so a tie never passes: close > SMA200,\nSPY close > SMA200, and SMA50 rising.\n\n',
         'average loss is 0. They are causal by construction: a prefix gives the same values. `analysis/` itself is not imported. The SMA is the correctly rounded mean\nof each window (`math.fsum` / n). The three strict comparisons with a mean (close > SMA200, SPY close > SMA200, and SMA50 rising) are\ndecided exactly on the stored double values, so a tie between those values never passes. A tie between decimal prices that the doubles do\nnot represent exactly can still pass, and a near-tie can be decided wrongly on a series whose early adjusted prices are about 10⁶ times its\nrecent ones\n(final-review M-1, wording corrected in Stage 7.8B-1; negligible on large-cap adjusted data; the behaviour is unchanged).\n\n'),
        ("  They are computed on the **TRAIN view** only (`dataset.until(train_end)`: no later bar exists), at the run's friction and sizing. A\n  candidate is purged by the harness's own boundary rule (its symbol's next 10 bars reach past the TRAIN end: dates only, never a price), so\n  the base row equals the run's own TRAIN result. The neighbourhood is labelled by the harness's own rule. A SENSITIVE or UNSTABLE label makes the result not promotable. The harness's own neighbour hook is not used, because it would evaluate\n",
         "  They are computed on the **TRAIN view** only (`dataset.until(train_end)`: no later bar exists), at the run's friction and sizing. A\n  candidate is purged by the harness's own boundary rule (its symbol's next `max_hold_bars` bars reach past the TRAIN end: 10, or 8 / 12 in the time-exit neighbours; dates only, never a price), so\n  the base row equals the run's own TRAIN result. The neighbourhood is labelled by the harness's own rule. A SENSITIVE or UNSTABLE label makes the result not promotable. The harness's own neighbour hook is not used, because it would evaluate\n"),
    ],
    'tests/test_bot_activity_68.py': [
        ('    new = {p for p in changed if p not in in_base}\n    assert new == FL.NEW_68 | FL.NEW_69 | FL.NEW_691 | FL.NEW_610A | FL.NEW_610C | FL.NEW_541 | FL.NEW_611FIX | FL.NEW_OPS | FL.NEW_610D | FL.NEW_542 | FL.NEW_70 | FL.NEW_71 | FL.NEW_72 | FL.NEW_73 | FL.NEW_731 | FL.NEW_74 | FL.NEW_75 | FL.NEW_78, {"unexpected_new": sorted(new - FL.NEW_68 - FL.NEW_69 - FL.NEW_691 - FL.NEW_610A - FL.NEW_610C - FL.NEW_541 - FL.NEW_611FIX - FL.NEW_OPS - FL.NEW_610D - FL.NEW_542 - FL.NEW_70 - FL.NEW_71 - FL.NEW_72 - FL.NEW_73 - FL.NEW_731 - FL.NEW_74 - FL.NEW_75 - FL.NEW_78),\n                                                                     "missing_new": sorted((FL.NEW_68 | FL.NEW_69 | FL.NEW_691 | FL.NEW_610A | FL.NEW_610C | FL.NEW_541 | FL.NEW_611FIX | FL.NEW_OPS | FL.NEW_610D | FL.NEW_542 | FL.NEW_70 | FL.NEW_71 | FL.NEW_72 | FL.NEW_73 | FL.NEW_731 | FL.NEW_74 | FL.NEW_75 | FL.NEW_78) - new)}   # + Stage 6.9 / 6.9.1 / 6.10A / 6.10C / 5.4.1\n    assert changed - new == set(FL.LEDGER), {"unregistered": sorted(changed - new - set(FL.LEDGER)), "unchanged_but_registered": sorted(set(FL.LEDGER) - (changed - new))}\n',
         '    new = {p for p in changed if p not in in_base}\n    assert new == FL.NEW_68 | FL.NEW_69 | FL.NEW_691 | FL.NEW_610A | FL.NEW_610C | FL.NEW_541 | FL.NEW_611FIX | FL.NEW_OPS | FL.NEW_610D | FL.NEW_542 | FL.NEW_70 | FL.NEW_71 | FL.NEW_72 | FL.NEW_73 | FL.NEW_731 | FL.NEW_74 | FL.NEW_75 | FL.NEW_78 | FL.NEW_78B1, {"unexpected_new": sorted(new - FL.NEW_68 - FL.NEW_69 - FL.NEW_691 - FL.NEW_610A - FL.NEW_610C - FL.NEW_541 - FL.NEW_611FIX - FL.NEW_OPS - FL.NEW_610D - FL.NEW_542 - FL.NEW_70 - FL.NEW_71 - FL.NEW_72 - FL.NEW_73 - FL.NEW_731 - FL.NEW_74 - FL.NEW_75 - FL.NEW_78 - FL.NEW_78B1),\n                                                                     "missing_new": sorted((FL.NEW_68 | FL.NEW_69 | FL.NEW_691 | FL.NEW_610A | FL.NEW_610C | FL.NEW_541 | FL.NEW_611FIX | FL.NEW_OPS | FL.NEW_610D | FL.NEW_542 | FL.NEW_70 | FL.NEW_71 | FL.NEW_72 | FL.NEW_73 | FL.NEW_731 | FL.NEW_74 | FL.NEW_75 | FL.NEW_78 | FL.NEW_78B1) - new)}   # + Stage 6.9 / 6.9.1 / 6.10A / 6.10C / 5.4.1\n    assert changed - new == set(FL.LEDGER), {"unregistered": sorted(changed - new - set(FL.LEDGER)), "unchanged_but_registered": sorted(set(FL.LEDGER) - (changed - new))}\n'),
    ],
    'tests/test_chart_empty_state_611fix.py': [
        ('    changed = {p for p in changed if not p.startswith(("scratchpad/", "runtime/", "data/", "logs/", "browser_tests/artifacts/")) and (ROOT / p).exists()}\n    assert changed == FL.NEW_611FIX | FL.EDITED_611FIX | FL.NEW_OPS | FL.EDITED_OPS | FL.NEW_610D | FL.EDITED_610D | FL.NEW_542 | FL.EDITED_542 | FL.NEW_70 | FL.EDITED_70 | FL.NEW_71 | FL.EDITED_71 | FL.NEW_72 | FL.EDITED_72 | FL.NEW_73 | FL.EDITED_73 | FL.NEW_731 | FL.EDITED_731 | FL.NEW_74 | FL.EDITED_74 | FL.NEW_75 | FL.EDITED_75 | FL.NEW_78 | FL.EDITED_78\n    changed = set(_git("diff", "--name-only", FL.BASE_611FIX, FL.BASE_OPS).split())\n',
         '    changed = {p for p in changed if not p.startswith(("scratchpad/", "runtime/", "data/", "logs/", "browser_tests/artifacts/")) and (ROOT / p).exists()}\n    assert changed == FL.NEW_611FIX | FL.EDITED_611FIX | FL.NEW_OPS | FL.EDITED_OPS | FL.NEW_610D | FL.EDITED_610D | FL.NEW_542 | FL.EDITED_542 | FL.NEW_70 | FL.EDITED_70 | FL.NEW_71 | FL.EDITED_71 | FL.NEW_72 | FL.EDITED_72 | FL.NEW_73 | FL.EDITED_73 | FL.NEW_731 | FL.EDITED_731 | FL.NEW_74 | FL.EDITED_74 | FL.NEW_75 | FL.EDITED_75 | FL.NEW_78 | FL.EDITED_78 | FL.NEW_78B1 | FL.EDITED_78B1\n    changed = set(_git("diff", "--name-only", FL.BASE_611FIX, FL.BASE_OPS).split())\n'),
    ],
    'tests/test_decision_flow_610c.py': [
        ('    changed = {p for p in changed if not p.startswith(("scratchpad/", "runtime/", "data/", "logs/", "browser_tests/artifacts/")) and (ROOT / p).exists()}\n    later = FL.NEW_541 | FL.EDITED_541 | FL.NEW_611FIX | FL.EDITED_611FIX | FL.NEW_OPS | FL.EDITED_OPS | FL.NEW_610D | FL.EDITED_610D | FL.NEW_542 | FL.EDITED_542 | FL.NEW_70 | FL.EDITED_70 | FL.NEW_71 | FL.EDITED_71 | FL.NEW_72 | FL.EDITED_72 | FL.NEW_73 | FL.EDITED_73 | FL.NEW_731 | FL.EDITED_731 | FL.NEW_74 | FL.EDITED_74 | FL.NEW_75 | FL.EDITED_75 | FL.NEW_78 | FL.EDITED_78                     # Stage 5.4.1 / the 6.1.1 CSS fix, each pinned by its own F tests\n    assert changed == FL.NEW_610C | FL.EDITED_610C | later, {"unexpected": sorted(changed - FL.NEW_610C - FL.EDITED_610C - later),\n',
         '    changed = {p for p in changed if not p.startswith(("scratchpad/", "runtime/", "data/", "logs/", "browser_tests/artifacts/")) and (ROOT / p).exists()}\n    later = FL.NEW_541 | FL.EDITED_541 | FL.NEW_611FIX | FL.EDITED_611FIX | FL.NEW_OPS | FL.EDITED_OPS | FL.NEW_610D | FL.EDITED_610D | FL.NEW_542 | FL.EDITED_542 | FL.NEW_70 | FL.EDITED_70 | FL.NEW_71 | FL.EDITED_71 | FL.NEW_72 | FL.EDITED_72 | FL.NEW_73 | FL.EDITED_73 | FL.NEW_731 | FL.EDITED_731 | FL.NEW_74 | FL.EDITED_74 | FL.NEW_75 | FL.EDITED_75 | FL.NEW_78 | FL.EDITED_78 | FL.NEW_78B1 | FL.EDITED_78B1                     # Stage 5.4.1 / the 6.1.1 CSS fix, each pinned by its own F tests\n    assert changed == FL.NEW_610C | FL.EDITED_610C | later, {"unexpected": sorted(changed - FL.NEW_610C - FL.EDITED_610C - later),\n'),
    ],
    'tests/test_engine_health_610d.py': [
        ('    assert stage == FL.NEW_610D | FL.EDITED_610D, {"unexpected": sorted(stage - FL.NEW_610D - FL.EDITED_610D), "missing": sorted(FL.NEW_610D | FL.EDITED_610D - stage)}\n    later = FL.NEW_542 | FL.EDITED_542 | FL.NEW_70 | FL.EDITED_70 | FL.NEW_71 | FL.EDITED_71 | FL.NEW_72 | FL.EDITED_72 | FL.NEW_73 | FL.EDITED_73 | FL.NEW_731 | FL.EDITED_731 | FL.NEW_74 | FL.EDITED_74 | FL.NEW_75 | FL.EDITED_75 | FL.NEW_78 | FL.EDITED_78                                                      # Stage 5.4.2, pinned by its own F tests\n    assert changed == FL.NEW_610D | FL.EDITED_610D | later, {"unexpected": sorted(changed - FL.NEW_610D - FL.EDITED_610D - later),\n',
         '    assert stage == FL.NEW_610D | FL.EDITED_610D, {"unexpected": sorted(stage - FL.NEW_610D - FL.EDITED_610D), "missing": sorted(FL.NEW_610D | FL.EDITED_610D - stage)}\n    later = FL.NEW_542 | FL.EDITED_542 | FL.NEW_70 | FL.EDITED_70 | FL.NEW_71 | FL.EDITED_71 | FL.NEW_72 | FL.EDITED_72 | FL.NEW_73 | FL.EDITED_73 | FL.NEW_731 | FL.EDITED_731 | FL.NEW_74 | FL.EDITED_74 | FL.NEW_75 | FL.EDITED_75 | FL.NEW_78 | FL.EDITED_78 | FL.NEW_78B1 | FL.EDITED_78B1                                                      # Stage 5.4.2, pinned by its own F tests\n    assert changed == FL.NEW_610D | FL.EDITED_610D | later, {"unexpected": sorted(changed - FL.NEW_610D - FL.EDITED_610D - later),\n'),
    ],
    'tests/test_evidence_gate_541.py': [
        ('    changed = {p for p in changed if not p.startswith(("scratchpad/", "runtime/", "data/", "logs/", "browser_tests/artifacts/")) and (ROOT / p).exists()}\n    later = FL.NEW_611FIX | FL.EDITED_611FIX | FL.NEW_OPS | FL.EDITED_OPS | FL.NEW_610D | FL.EDITED_610D | FL.NEW_542 | FL.EDITED_542 | FL.NEW_70 | FL.EDITED_70 | FL.NEW_71 | FL.EDITED_71 | FL.NEW_72 | FL.EDITED_72 | FL.NEW_73 | FL.EDITED_73 | FL.NEW_731 | FL.EDITED_731 | FL.NEW_74 | FL.EDITED_74 | FL.NEW_75 | FL.EDITED_75 | FL.NEW_78 | FL.EDITED_78                                                  # the Stage 6.1.1 CSS fix, pinned by its own F tests\n    assert changed == FL.NEW_541 | FL.EDITED_541 | later, {"unexpected": sorted(changed - FL.NEW_541 - FL.EDITED_541 - later), "missing": sorted((FL.NEW_541 | FL.EDITED_541 | later) - changed)}\n',
         '    changed = {p for p in changed if not p.startswith(("scratchpad/", "runtime/", "data/", "logs/", "browser_tests/artifacts/")) and (ROOT / p).exists()}\n    later = FL.NEW_611FIX | FL.EDITED_611FIX | FL.NEW_OPS | FL.EDITED_OPS | FL.NEW_610D | FL.EDITED_610D | FL.NEW_542 | FL.EDITED_542 | FL.NEW_70 | FL.EDITED_70 | FL.NEW_71 | FL.EDITED_71 | FL.NEW_72 | FL.EDITED_72 | FL.NEW_73 | FL.EDITED_73 | FL.NEW_731 | FL.EDITED_731 | FL.NEW_74 | FL.EDITED_74 | FL.NEW_75 | FL.EDITED_75 | FL.NEW_78 | FL.EDITED_78 | FL.NEW_78B1 | FL.EDITED_78B1                                                  # the Stage 6.1.1 CSS fix, pinned by its own F tests\n    assert changed == FL.NEW_541 | FL.EDITED_541 | later, {"unexpected": sorted(changed - FL.NEW_541 - FL.EDITED_541 - later), "missing": sorted((FL.NEW_541 | FL.EDITED_541 | later) - changed)}\n'),
    ],
    'tests/test_evidence_refresh_542.py': [
        ('    assert stage == FL.NEW_542 | FL.EDITED_542, {"unexpected": sorted(stage - FL.NEW_542 - FL.EDITED_542), "missing": sorted((FL.NEW_542 | FL.EDITED_542) - stage)}\n    later = FL.NEW_70 | FL.EDITED_70 | FL.NEW_71 | FL.EDITED_71 | FL.NEW_72 | FL.EDITED_72 | FL.NEW_73 | FL.EDITED_73 | FL.NEW_731 | FL.EDITED_731 | FL.NEW_74 | FL.EDITED_74 | FL.NEW_75 | FL.EDITED_75 | FL.NEW_78 | FL.EDITED_78                                                         # Stage 7.0, pinned by its own F tests\n    assert changed == FL.NEW_542 | FL.EDITED_542 | later, {"unexpected": sorted(changed - FL.NEW_542 - FL.EDITED_542 - later),\n',
         '    assert stage == FL.NEW_542 | FL.EDITED_542, {"unexpected": sorted(stage - FL.NEW_542 - FL.EDITED_542), "missing": sorted((FL.NEW_542 | FL.EDITED_542) - stage)}\n    later = FL.NEW_70 | FL.EDITED_70 | FL.NEW_71 | FL.EDITED_71 | FL.NEW_72 | FL.EDITED_72 | FL.NEW_73 | FL.EDITED_73 | FL.NEW_731 | FL.EDITED_731 | FL.NEW_74 | FL.EDITED_74 | FL.NEW_75 | FL.EDITED_75 | FL.NEW_78 | FL.EDITED_78 | FL.NEW_78B1 | FL.EDITED_78B1                                                         # Stage 7.0, pinned by its own F tests\n    assert changed == FL.NEW_542 | FL.EDITED_542 | later, {"unexpected": sorted(changed - FL.NEW_542 - FL.EDITED_542 - later),\n'),
    ],
    'tests/test_forward_paper_610a.py': [
        ('    changed = {p for p in changed if not p.startswith(("scratchpad/", "runtime/", "data/", "logs/", "browser_tests/artifacts/")) and (ROOT / p).exists()}\n    later = FL.NEW_610C | FL.EDITED_610C | FL.NEW_541 | FL.EDITED_541 | FL.NEW_611FIX | FL.EDITED_611FIX | FL.NEW_OPS | FL.EDITED_OPS | FL.NEW_610D | FL.EDITED_610D | FL.NEW_542 | FL.EDITED_542 | FL.NEW_70 | FL.EDITED_70 | FL.NEW_71 | FL.EDITED_71 | FL.NEW_72 | FL.EDITED_72 | FL.NEW_73 | FL.EDITED_73 | FL.NEW_731 | FL.EDITED_731 | FL.NEW_74 | FL.EDITED_74 | FL.NEW_75 | FL.EDITED_75 | FL.NEW_78 | FL.EDITED_78   # Stage 6.10C / 5.4.1 / the 6.1.1 CSS fix, each pinned by its own F tests\n    assert changed == FL.NEW_610A | FL.EDITED_610A | later, {"unexpected": sorted(changed - FL.NEW_610A - FL.EDITED_610A - later),\n',
         '    changed = {p for p in changed if not p.startswith(("scratchpad/", "runtime/", "data/", "logs/", "browser_tests/artifacts/")) and (ROOT / p).exists()}\n    later = FL.NEW_610C | FL.EDITED_610C | FL.NEW_541 | FL.EDITED_541 | FL.NEW_611FIX | FL.EDITED_611FIX | FL.NEW_OPS | FL.EDITED_OPS | FL.NEW_610D | FL.EDITED_610D | FL.NEW_542 | FL.EDITED_542 | FL.NEW_70 | FL.EDITED_70 | FL.NEW_71 | FL.EDITED_71 | FL.NEW_72 | FL.EDITED_72 | FL.NEW_73 | FL.EDITED_73 | FL.NEW_731 | FL.EDITED_731 | FL.NEW_74 | FL.EDITED_74 | FL.NEW_75 | FL.EDITED_75 | FL.NEW_78 | FL.EDITED_78 | FL.NEW_78B1 | FL.EDITED_78B1   # Stage 6.10C / 5.4.1 / the 6.1.1 CSS fix, each pinned by its own F tests\n    assert changed == FL.NEW_610A | FL.EDITED_610A | later, {"unexpected": sorted(changed - FL.NEW_610A - FL.EDITED_610A - later),\n'),
    ],
    'tests/test_market_intel_70.py': [
        ('    assert stage == FL.NEW_70 | FL.EDITED_70, {"unexpected": sorted(stage - FL.NEW_70 - FL.EDITED_70), "missing": sorted((FL.NEW_70 | FL.EDITED_70) - stage)}\n    later = FL.NEW_71 | FL.EDITED_71 | FL.NEW_72 | FL.EDITED_72 | FL.NEW_73 | FL.EDITED_73 | FL.NEW_731 | FL.EDITED_731 | FL.NEW_74 | FL.EDITED_74 | FL.NEW_75 | FL.EDITED_75 | FL.NEW_78 | FL.EDITED_78   # Stages 7.1-7.3: pinned by their own F tests\n    assert changed == FL.NEW_70 | FL.EDITED_70 | later, {"unexpected": sorted(changed - FL.NEW_70 - FL.EDITED_70 - later),\n',
         '    assert stage == FL.NEW_70 | FL.EDITED_70, {"unexpected": sorted(stage - FL.NEW_70 - FL.EDITED_70), "missing": sorted((FL.NEW_70 | FL.EDITED_70) - stage)}\n    later = FL.NEW_71 | FL.EDITED_71 | FL.NEW_72 | FL.EDITED_72 | FL.NEW_73 | FL.EDITED_73 | FL.NEW_731 | FL.EDITED_731 | FL.NEW_74 | FL.EDITED_74 | FL.NEW_75 | FL.EDITED_75 | FL.NEW_78 | FL.EDITED_78 | FL.NEW_78B1 | FL.EDITED_78B1   # Stages 7.1-7.3: pinned by their own F tests\n    assert changed == FL.NEW_70 | FL.EDITED_70 | later, {"unexpected": sorted(changed - FL.NEW_70 - FL.EDITED_70 - later),\n'),
    ],
    'tests/test_market_intel_collector_73.py': [
        ('    assert stage == FL.NEW_73 | FL.EDITED_73, {"unexpected": sorted(stage - FL.NEW_73 - FL.EDITED_73), "missing": sorted((FL.NEW_73 | FL.EDITED_73) - stage)}\n    later = FL.NEW_731 | FL.EDITED_731 | FL.NEW_74 | FL.EDITED_74 | FL.NEW_75 | FL.EDITED_75 | FL.NEW_78 | FL.EDITED_78                           # Stages 7.3.1, 7.4: pinned by their own F tests\n    assert changed == FL.NEW_73 | FL.EDITED_73 | later, {"unexpected": sorted(changed - FL.NEW_73 - FL.EDITED_73 - later),\n',
         '    assert stage == FL.NEW_73 | FL.EDITED_73, {"unexpected": sorted(stage - FL.NEW_73 - FL.EDITED_73), "missing": sorted((FL.NEW_73 | FL.EDITED_73) - stage)}\n    later = FL.NEW_731 | FL.EDITED_731 | FL.NEW_74 | FL.EDITED_74 | FL.NEW_75 | FL.EDITED_75 | FL.NEW_78 | FL.EDITED_78 | FL.NEW_78B1 | FL.EDITED_78B1                           # Stages 7.3.1, 7.4: pinned by their own F tests\n    assert changed == FL.NEW_73 | FL.EDITED_73 | later, {"unexpected": sorted(changed - FL.NEW_73 - FL.EDITED_73 - later),\n'),
    ],
    'tests/test_market_intel_quality_74.py': [
        ('    assert stage == FL.NEW_74 | FL.EDITED_74, {"unexpected": sorted(stage - FL.NEW_74 - FL.EDITED_74), "missing": sorted((FL.NEW_74 | FL.EDITED_74) - stage)}\n    later = FL.NEW_75 | FL.EDITED_75 | FL.NEW_78 | FL.EDITED_78                                                         # Stage 7.5, pinned by its own F tests\n    assert changed == FL.NEW_74 | FL.EDITED_74 | later, {"unexpected": sorted(changed - FL.NEW_74 - FL.EDITED_74 - later),\n',
         '    assert stage == FL.NEW_74 | FL.EDITED_74, {"unexpected": sorted(stage - FL.NEW_74 - FL.EDITED_74), "missing": sorted((FL.NEW_74 | FL.EDITED_74) - stage)}\n    later = FL.NEW_75 | FL.EDITED_75 | FL.NEW_78 | FL.EDITED_78 | FL.NEW_78B1 | FL.EDITED_78B1                                                         # Stage 7.5, pinned by its own F tests\n    assert changed == FL.NEW_74 | FL.EDITED_74 | later, {"unexpected": sorted(changed - FL.NEW_74 - FL.EDITED_74 - later),\n'),
    ],
    'tests/test_market_intel_schema_v2_731.py': [
        ('    assert stage == FL.NEW_731 | FL.EDITED_731, {"unexpected": sorted(stage - FL.NEW_731 - FL.EDITED_731), "missing": sorted((FL.NEW_731 | FL.EDITED_731) - stage)}\n    later = FL.NEW_74 | FL.EDITED_74 | FL.NEW_75 | FL.EDITED_75 | FL.NEW_78 | FL.EDITED_78                                                         # Stage 7.4, pinned by its own F tests\n    assert changed == FL.NEW_731 | FL.EDITED_731 | later, {"unexpected": sorted(changed - FL.NEW_731 - FL.EDITED_731 - later),\n',
         '    assert stage == FL.NEW_731 | FL.EDITED_731, {"unexpected": sorted(stage - FL.NEW_731 - FL.EDITED_731), "missing": sorted((FL.NEW_731 | FL.EDITED_731) - stage)}\n    later = FL.NEW_74 | FL.EDITED_74 | FL.NEW_75 | FL.EDITED_75 | FL.NEW_78 | FL.EDITED_78 | FL.NEW_78B1 | FL.EDITED_78B1                                                         # Stage 7.4, pinned by its own F tests\n    assert changed == FL.NEW_731 | FL.EDITED_731 | later, {"unexpected": sorted(changed - FL.NEW_731 - FL.EDITED_731 - later),\n'),
    ],
    'tests/test_market_intel_sources_72.py': [
        ('    assert stage == FL.NEW_72 | FL.EDITED_72, {"unexpected": sorted(stage - FL.NEW_72 - FL.EDITED_72), "missing": sorted((FL.NEW_72 | FL.EDITED_72) - stage)}\n    later = FL.NEW_73 | FL.EDITED_73 | FL.NEW_731 | FL.EDITED_731 | FL.NEW_74 | FL.EDITED_74 | FL.NEW_75 | FL.EDITED_75 | FL.NEW_78 | FL.EDITED_78                           # Stages 7.3, 7.3.1: pinned by their own F tests\n    assert changed == FL.NEW_72 | FL.EDITED_72 | later, {"unexpected": sorted(changed - FL.NEW_72 - FL.EDITED_72 - later),\n',
         '    assert stage == FL.NEW_72 | FL.EDITED_72, {"unexpected": sorted(stage - FL.NEW_72 - FL.EDITED_72), "missing": sorted((FL.NEW_72 | FL.EDITED_72) - stage)}\n    later = FL.NEW_73 | FL.EDITED_73 | FL.NEW_731 | FL.EDITED_731 | FL.NEW_74 | FL.EDITED_74 | FL.NEW_75 | FL.EDITED_75 | FL.NEW_78 | FL.EDITED_78 | FL.NEW_78B1 | FL.EDITED_78B1                           # Stages 7.3, 7.3.1: pinned by their own F tests\n    assert changed == FL.NEW_72 | FL.EDITED_72 | later, {"unexpected": sorted(changed - FL.NEW_72 - FL.EDITED_72 - later),\n'),
    ],
    'tests/test_market_intel_store_71.py': [
        ('    assert stage == FL.NEW_71 | FL.EDITED_71, {"unexpected": sorted(stage - FL.NEW_71 - FL.EDITED_71), "missing": sorted((FL.NEW_71 | FL.EDITED_71) - stage)}\n    later = FL.NEW_72 | FL.EDITED_72 | FL.NEW_73 | FL.EDITED_73 | FL.NEW_731 | FL.EDITED_731 | FL.NEW_74 | FL.EDITED_74 | FL.NEW_75 | FL.EDITED_75 | FL.NEW_78 | FL.EDITED_78                             # Stages 7.2, 7.3: pinned by their own F tests\n    assert changed == FL.NEW_71 | FL.EDITED_71 | later, {"unexpected": sorted(changed - FL.NEW_71 - FL.EDITED_71 - later),\n',
         '    assert stage == FL.NEW_71 | FL.EDITED_71, {"unexpected": sorted(stage - FL.NEW_71 - FL.EDITED_71), "missing": sorted((FL.NEW_71 | FL.EDITED_71) - stage)}\n    later = FL.NEW_72 | FL.EDITED_72 | FL.NEW_73 | FL.EDITED_73 | FL.NEW_731 | FL.EDITED_731 | FL.NEW_74 | FL.EDITED_74 | FL.NEW_75 | FL.EDITED_75 | FL.NEW_78 | FL.EDITED_78 | FL.NEW_78B1 | FL.EDITED_78B1                             # Stages 7.2, 7.3: pinned by their own F tests\n    assert changed == FL.NEW_71 | FL.EDITED_71 | later, {"unexpected": sorted(changed - FL.NEW_71 - FL.EDITED_71 - later),\n'),
    ],
    'tests/test_market_scan_75.py': [
        ('    assert stage == FL.NEW_75 | FL.EDITED_75, {"unexpected": sorted(stage - FL.NEW_75 - FL.EDITED_75), "missing": sorted((FL.NEW_75 | FL.EDITED_75) - stage)}\n    later = FL.NEW_78 | FL.EDITED_78                                                         # Stage 7.8, pinned by its own F tests\n    assert changed == FL.NEW_75 | FL.EDITED_75 | later, {"unexpected": sorted(changed - FL.NEW_75 - FL.EDITED_75 - later),\n',
         '    assert stage == FL.NEW_75 | FL.EDITED_75, {"unexpected": sorted(stage - FL.NEW_75 - FL.EDITED_75), "missing": sorted((FL.NEW_75 | FL.EDITED_75) - stage)}\n    later = FL.NEW_78 | FL.EDITED_78 | FL.NEW_78B1 | FL.EDITED_78B1                           # Stages 7.8, 7.8B-1: pinned by their own F tests\n    assert changed == FL.NEW_75 | FL.EDITED_75 | later, {"unexpected": sorted(changed - FL.NEW_75 - FL.EDITED_75 - later),\n'),
    ],
    'tests/test_robinhood_portfolio_69.py': [
        ('    changed = {p for p in changed if not p.startswith(("scratchpad/", "runtime/", "data/", "logs/", "browser_tests/artifacts/")) and (ROOT / p).exists()}\n    later = FL.NEW_691 | FL.EDITED_691 | FL.NEW_610A | FL.EDITED_610A | FL.NEW_610C | FL.EDITED_610C | FL.NEW_541 | FL.EDITED_541 | FL.NEW_611FIX | FL.EDITED_611FIX | FL.NEW_OPS | FL.EDITED_OPS | FL.NEW_610D | FL.EDITED_610D | FL.NEW_542 | FL.EDITED_542 | FL.NEW_70 | FL.EDITED_70 | FL.NEW_71 | FL.EDITED_71 | FL.NEW_72 | FL.EDITED_72 | FL.NEW_73 | FL.EDITED_73 | FL.NEW_731 | FL.EDITED_731 | FL.NEW_74 | FL.EDITED_74 | FL.NEW_75 | FL.EDITED_75 | FL.NEW_78 | FL.EDITED_78   # the 6.1.1 CSS fix, Stage 6.9.1 (FL.SEMANTIC_691), Stage 6.10A (FL.SEMANTIC_610A), Stage 6.10C, Stage 5.4.1 (FL.SEMANTIC_541),\n    assert changed == NEW_69_FILES | EDITED_69 | later, {"unexpected": sorted(changed - NEW_69_FILES - EDITED_69 - later),   # pinned by tests/test_unified_exposure_gate_691.py\n',
         '    changed = {p for p in changed if not p.startswith(("scratchpad/", "runtime/", "data/", "logs/", "browser_tests/artifacts/")) and (ROOT / p).exists()}\n    later = FL.NEW_691 | FL.EDITED_691 | FL.NEW_610A | FL.EDITED_610A | FL.NEW_610C | FL.EDITED_610C | FL.NEW_541 | FL.EDITED_541 | FL.NEW_611FIX | FL.EDITED_611FIX | FL.NEW_OPS | FL.EDITED_OPS | FL.NEW_610D | FL.EDITED_610D | FL.NEW_542 | FL.EDITED_542 | FL.NEW_70 | FL.EDITED_70 | FL.NEW_71 | FL.EDITED_71 | FL.NEW_72 | FL.EDITED_72 | FL.NEW_73 | FL.EDITED_73 | FL.NEW_731 | FL.EDITED_731 | FL.NEW_74 | FL.EDITED_74 | FL.NEW_75 | FL.EDITED_75 | FL.NEW_78 | FL.EDITED_78 | FL.NEW_78B1 | FL.EDITED_78B1   # the 6.1.1 CSS fix, Stage 6.9.1 (FL.SEMANTIC_691), Stage 6.10A (FL.SEMANTIC_610A), Stage 6.10C, Stage 5.4.1 (FL.SEMANTIC_541),\n    assert changed == NEW_69_FILES | EDITED_69 | later, {"unexpected": sorted(changed - NEW_69_FILES - EDITED_69 - later),   # pinned by tests/test_unified_exposure_gate_691.py\n'),
    ],
    'tests/test_runtime_reliability_ops.py': [
        ('    changed = {p for p in changed if not p.startswith(("scratchpad/", "runtime/", "data/", "logs/", "browser_tests/artifacts/")) and (ROOT / p).exists()}\n    assert changed == FL.NEW_OPS | FL.EDITED_OPS | FL.NEW_610D | FL.EDITED_610D | FL.NEW_542 | FL.EDITED_542 | FL.NEW_70 | FL.EDITED_70 | FL.NEW_71 | FL.EDITED_71 | FL.NEW_72 | FL.EDITED_72 | FL.NEW_73 | FL.EDITED_73 | FL.NEW_731 | FL.EDITED_731 | FL.NEW_74 | FL.EDITED_74 | FL.NEW_75 | FL.EDITED_75 | FL.NEW_78 | FL.EDITED_78\n    changed = set(_git("diff", "--name-only", FL.BASE_OPS, FL.BASE_610D).split())\n',
         '    changed = {p for p in changed if not p.startswith(("scratchpad/", "runtime/", "data/", "logs/", "browser_tests/artifacts/")) and (ROOT / p).exists()}\n    assert changed == FL.NEW_OPS | FL.EDITED_OPS | FL.NEW_610D | FL.EDITED_610D | FL.NEW_542 | FL.EDITED_542 | FL.NEW_70 | FL.EDITED_70 | FL.NEW_71 | FL.EDITED_71 | FL.NEW_72 | FL.EDITED_72 | FL.NEW_73 | FL.EDITED_73 | FL.NEW_731 | FL.EDITED_731 | FL.NEW_74 | FL.EDITED_74 | FL.NEW_75 | FL.EDITED_75 | FL.NEW_78 | FL.EDITED_78 | FL.NEW_78B1 | FL.EDITED_78B1\n    changed = set(_git("diff", "--name-only", FL.BASE_OPS, FL.BASE_610D).split())\n'),
    ],
    'tests/test_swing_pullback_78.py': [
        ('        rel = p.relative_to(ROOT).as_posix()\n        if rel.startswith(("swing_pullback/", "tests/", ".venv/", "scratchpad/", ".claude/")) or "/site-packages/" in rel:\n            continue\n        assert "swing_pullback" not in p.read_text(encoding="utf-8", errors="replace"), rel\n',
         '        rel = p.relative_to(ROOT).as_posix()\n        if rel.startswith(("swing_pullback/", "swing_eval/", "tests/", ".venv/", "scratchpad/", ".claude/")) or "/site-packages/" in rel:\n            continue                                                                     # swing_eval/: the Stage 7.8B-1 offline evaluator\n        assert "swing_pullback" not in p.read_text(encoding="utf-8", errors="replace"), rel\n'),
        ('\nNEW_MODULES_78 = ("__init__.py", "adapter.py", "core.py", "data.py", "diagnostics.py", "indicators.py", "plan.py")\n',
         '\ndef post(rel):\n    """main after PR #48 (Stage 7.8 merged; the Stage 7.8B-1 base): what Stage 7.8 left, whatever a later stage changed since."""\n    run = subprocess.run(["git", "show", f"{FL.BASE_78B1}:{rel}"], cwd=ROOT, capture_output=True)\n    assert run.returncode == 0, f"{rel} is not in {FL.BASE_78B1} (fetch-depth 0 is required)"\n    return run.stdout.decode("utf-8").replace("\\r\\n", "\\n")\n\n\nNEW_MODULES_78 = ("__init__.py", "adapter.py", "core.py", "data.py", "diagnostics.py", "indicators.py", "plan.py")\n'),
        ('    changed = {p for p in changed if not p.startswith(("scratchpad/", "runtime/", "data/market_intel", "logs/", "browser_tests/artifacts/")) and (ROOT / p).exists()}\n    assert changed == FL.NEW_78 | FL.EDITED_78, {"unexpected": sorted(changed - FL.NEW_78 - FL.EDITED_78), "missing": sorted((FL.NEW_78 | FL.EDITED_78) - changed)}\n    in_base = set(_git("ls-tree", "-r", "--name-only", FL.BASE_78).split())\n',
         '    changed = {p for p in changed if not p.startswith(("scratchpad/", "runtime/", "data/market_intel", "logs/", "browser_tests/artifacts/")) and (ROOT / p).exists()}\n    stage = set(_git("diff", "--name-only", FL.BASE_78, FL.BASE_78B1).split())               # Stage 7.8 itself, between two fixed commits\n    assert stage == FL.NEW_78 | FL.EDITED_78, {"unexpected": sorted(stage - FL.NEW_78 - FL.EDITED_78), "missing": sorted((FL.NEW_78 | FL.EDITED_78) - stage)}\n    later = FL.NEW_78B1 | FL.EDITED_78B1                                                     # Stage 7.8B-1, pinned by its own F tests\n    assert changed == FL.NEW_78 | FL.EDITED_78 | later, {"unexpected": sorted(changed - FL.NEW_78 - FL.EDITED_78 - later),\n                                                         "missing": sorted((FL.NEW_78 | FL.EDITED_78 | later) - changed)}\n    in_base = set(_git("ls-tree", "-r", "--name-only", FL.BASE_78).split())\n'),
        ('    in_base = set(_git("ls-tree", "-r", "--name-only", FL.BASE_78).split())\n    assert {p for p in changed if p not in in_base} == FL.NEW_78 and {p for p in changed if p in in_base} == FL.EDITED_78\n    assert FL.NEW_78 == ({"DESIGN_78_SWING_PULLBACK.md", "tests/test_swing_pullback_78.py", "tests/swing_pullback_fixtures.py"}\n',
         '    in_base = set(_git("ls-tree", "-r", "--name-only", FL.BASE_78).split())\n    assert {p for p in stage if p not in in_base} == FL.NEW_78 and {p for p in stage if p in in_base} == FL.EDITED_78\n    assert FL.NEW_78 == ({"DESIGN_78_SWING_PULLBACK.md", "tests/test_swing_pullback_78.py", "tests/swing_pullback_fixtures.py"}\n'),
        ('            if p.is_file() and rel in in_base and "__pycache__" not in rel:\n                assert now_text(rel) == at_base(rel), rel\n    for rel in ("config.py", "api/server.py", "autorun/calendar.py", "autorun/session.py", ".gitignore", "requirements.txt", "watchlist.txt",\n',
         '            if p.is_file() and rel in in_base and "__pycache__" not in rel:\n                assert post(rel) == at_base(rel), rel\n    for rel in ("config.py", "api/server.py", "autorun/calendar.py", "autorun/session.py", ".gitignore", "requirements.txt", "watchlist.txt",\n'),
        ('                "STRATEGY_STANDARD_V1.md", "STRATEGY_VALIDATION_HARNESS.md", "main.py", ".github/workflows/tests.yml"):\n        assert now_text(rel) == at_base(rel), rel                                          # no config key, route, lifespan hook, dependency or workflow edit\n    for rel in sorted(FL.NEW_78 | FL.EDITED_78):\n',
         '                "STRATEGY_STANDARD_V1.md", "STRATEGY_VALIDATION_HARNESS.md", "main.py", ".github/workflows/tests.yml"):\n        assert post(rel) == at_base(rel), rel                                              # no config key, route, lifespan hook, dependency or workflow edit\n    for rel in sorted(FL.NEW_78 | FL.EDITED_78):\n'),
        ('            m61, m54 = "# >>> " + "Stage 6.1", "# >>> " + "Stage 5.4"                    # built, so this file never carries the markers itself\n            assert m61 not in now_text(rel) and m54 not in now_text(rel), rel\n\n',
         '            m61, m54 = "# >>> " + "Stage 6.1", "# >>> " + "Stage 5.4"                    # built, so this file never carries the markers itself\n            assert m61 not in post(rel) and m54 not in post(rel), rel\n\n'),
        ('    for rel, edits in REPLAY_78.items():\n        text = now_text(rel)\n        for old, new in edits:\n',
         '    for rel, edits in REPLAY_78.items():\n        text = post(rel)\n        for old, new in edits:\n'),
        ('    assert set(REPLAY_78) == FL.EDITED_78 - set(FL.LEDGER) - {"tests/freeze_ledger.py"}     # every other edited file carries a ledger entry\n    fl, base = now_text("tests/freeze_ledger.py"), at_base("tests/freeze_ledger.py")\n    start = fl.index("\\nStage 7.8 Part B (the swing trend pullback")\n',
         '    assert set(REPLAY_78) == FL.EDITED_78 - set(FL.LEDGER) - {"tests/freeze_ledger.py"}     # every other edited file carries a ledger entry\n    fl, base = post("tests/freeze_ledger.py"), at_base("tests/freeze_ledger.py")\n    start = fl.index("\\nStage 7.8 Part B (the swing trend pullback")\n'),
        ('    old = ast.literal_eval(led(base)[len("LEDGER = "):])\n    assert set(FL.LEDGER) == set(old)                                                       # no new ledger key\n    assert {k for k in old if old[k] != FL.LEDGER[k]} == {".github/ci/pytest_groups.json", "tests/test_strategy_validation_62.py"} == FL.EDITED_78 & set(old)\n    for rel in FL.LEDGER:\n',
         '    old = ast.literal_eval(led(base)[len("LEDGER = "):])\n    new_led = ast.literal_eval(led(fl)[len("LEDGER = "):])                                 # the ledger Stage 7.8 left\n    assert set(new_led) == set(old)                                                         # no new ledger key\n    assert {k for k in old if old[k] != new_led[k]} == {".github/ci/pytest_groups.json", "tests/test_strategy_validation_62.py"} == FL.EDITED_78 & set(old)\n    for rel in FL.LEDGER:\n'),
    ],
    'tests/test_unified_exposure_gate_691.py': [
        ('    changed = {p for p in changed if not p.startswith(("scratchpad/", "runtime/", "data/", "logs/", "browser_tests/artifacts/")) and (ROOT / p).exists()}\n    later = FL.NEW_610A | FL.EDITED_610A | FL.NEW_610C | FL.EDITED_610C | FL.NEW_541 | FL.EDITED_541 | FL.NEW_611FIX | FL.EDITED_611FIX | FL.NEW_OPS | FL.EDITED_OPS | FL.NEW_610D | FL.EDITED_610D | FL.NEW_542 | FL.EDITED_542 | FL.NEW_70 | FL.EDITED_70 | FL.NEW_71 | FL.EDITED_71 | FL.NEW_72 | FL.EDITED_72 | FL.NEW_73 | FL.EDITED_73 | FL.NEW_731 | FL.EDITED_731 | FL.NEW_74 | FL.EDITED_74 | FL.NEW_75 | FL.EDITED_75 | FL.NEW_78 | FL.EDITED_78   # Stage 6.10A / 6.10C / 5.4.1 / the 6.1.1 CSS fix, each pinned by its own F tests\n    assert changed == FL.NEW_691 | FL.EDITED_691 | later, {"unexpected": sorted(changed - FL.NEW_691 - FL.EDITED_691 - later),\n',
         '    changed = {p for p in changed if not p.startswith(("scratchpad/", "runtime/", "data/", "logs/", "browser_tests/artifacts/")) and (ROOT / p).exists()}\n    later = FL.NEW_610A | FL.EDITED_610A | FL.NEW_610C | FL.EDITED_610C | FL.NEW_541 | FL.EDITED_541 | FL.NEW_611FIX | FL.EDITED_611FIX | FL.NEW_OPS | FL.EDITED_OPS | FL.NEW_610D | FL.EDITED_610D | FL.NEW_542 | FL.EDITED_542 | FL.NEW_70 | FL.EDITED_70 | FL.NEW_71 | FL.EDITED_71 | FL.NEW_72 | FL.EDITED_72 | FL.NEW_73 | FL.EDITED_73 | FL.NEW_731 | FL.EDITED_731 | FL.NEW_74 | FL.EDITED_74 | FL.NEW_75 | FL.EDITED_75 | FL.NEW_78 | FL.EDITED_78 | FL.NEW_78B1 | FL.EDITED_78B1   # Stage 6.10A / 6.10C / 5.4.1 / the 6.1.1 CSS fix, each pinned by its own F tests\n    assert changed == FL.NEW_691 | FL.EDITED_691 | later, {"unexpected": sorted(changed - FL.NEW_691 - FL.EDITED_691 - later),\n'),
    ],
}
