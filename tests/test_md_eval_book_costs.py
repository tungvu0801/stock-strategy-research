"""
MD-B (md_eval): the FROZEN book, the cost model and the cash rate.
  K01-K07  4 positions / 2 new a session / 2 a sector with NOT_TAKEN reasons; deterministic tie-break (permutation invariant); 0.5 % risk on
           CURRENT equity; the 10 % cap; invalid stops; purge and embargo; the book cannot be overridden; fill-time limits; under one share
  F01-F06  dated SEC Section 31 and FINRA TAF (cap, holiday, missing history -> maximum + flag), CAT and commission flags, buckets as
           ASSUMPTION defaults, 1x / 2x (friction only), break-even; an engine run with a sale carries the fee flags
  R01-R02  the cash rate: ZERO base, HYPOTHETICAL constant labelled, an unregistered series refused
"""
import math

import pytest

import md_eval
import md_eval_fixtures as FX
from md_eval import book as BK
from md_eval import cash_rate as CR
from md_eval import costs as CO
from md_eval import engine as EN
from md_eval.signals import Signal

SECTORS = {"SIM:A": "TECH", "SIM:B": "TECH", "SIM:C": "TECH", "SIM:D": "HEALTH", "SIM:E": "HEALTH", "SIM:F": "ENERGY"}


def _book_double(n=12, prices=None):
    return FX.simple_double(prices or {k: FX.flat(n) for k in SECTORS}, n=n, sectors=SECTORS)


def _strat(iid, stop=90.0, close=100.0, hold=None, score=0.0):
    return Signal(iid, score=score, raw_close=close, stop_raw=stop, target_raw=None, max_hold=hold)


def _run(d, src, **kw):
    kw.setdefault("cost_model", FX.zero_costs())
    kw.setdefault("initial_cash", 10_000.0)
    return EN.run(d, src, FX.v1_config(d, **kw))


def _dec(r):
    return sorted((x["session"], x["instrument_id"], x["action"], x["reason"], x.get("notional")) for x in r["decisions"])


# ============================================================================================================ the book (K)
def test_K01_positions_new_entries_and_sector_limits_with_reasons():
    d = _book_double()
    s = d.sessions("0", "9")
    entries = {1: [_strat(k) for k in sorted(SECTORS)], 2: [_strat(k) for k in ("SIM:C", "SIM:D", "SIM:E", "SIM:F")], 3: [_strat("SIM:F"), _strat("SIM:A")]}
    r = _run(d, FX.ScriptedSource(s, entries=entries, role="STRATEGY"))
    want = sorted([(s[1], "SIM:A", "ENTRY", "SIGNAL", 500.0), (s[1], "SIM:B", "ENTRY", "SIGNAL", 500.0)]
                  + [(s[1], k, "NOT_TAKEN", "MAX_NEW_PER_SESSION", None) for k in ("SIM:C", "SIM:D", "SIM:E", "SIM:F")]
                  + [(s[2], "SIM:C", "NOT_TAKEN", "MAX_PER_SECTOR", None), (s[2], "SIM:D", "ENTRY", "SIGNAL", 500.0),
                     (s[2], "SIM:E", "ENTRY", "SIGNAL", 500.0), (s[2], "SIM:F", "NOT_TAKEN", "MAX_NEW_PER_SESSION", None)]
                  + [(s[3], "SIM:F", "NOT_TAKEN", "MAX_POSITIONS", None), (s[3], "SIM:A", "NOT_TAKEN", "ALREADY_HELD", None)])
    assert _dec(r) == want
    assert sorted((f["instrument_id"], f["shares"]) for f in r["fills"]) == [("SIM:A", 5), ("SIM:B", 5), ("SIM:D", 5), ("SIM:E", 5)]
    assert len(r["open_positions"]) == md_eval.BOOK["max_positions"] == 4 and all(row["cash"] >= 0 for row in r["rows"])


def test_K02_tie_breaks_are_deterministic_and_independent_of_input_order():
    d = _book_double()
    s = d.sessions("0", "9")
    fwd = _run(d, FX.ScriptedSource(s, entries={1: [_strat(k) for k in sorted(SECTORS)]}, role="STRATEGY"))
    rev = _run(d, FX.ScriptedSource(s, entries={1: [_strat(k) for k in sorted(SECTORS, reverse=True)]}, role="STRATEGY"))
    assert _dec(fwd) == _dec(rev) and fwd["fills"] == rev["fills"]
    scored = _run(d, FX.ScriptedSource(s, entries={1: [_strat("SIM:A"), _strat("SIM:F", score=9.0), _strat("SIM:E", score=5.0)]}, role="STRATEGY"))
    assert [x["instrument_id"] for x in scored["decisions"] if x["action"] == "ENTRY"] == ["SIM:F", "SIM:E"]      # score first, then the id


def test_K03_risk_is_half_a_percent_of_current_equity_capped_at_ten_percent():
    n = 12
    prices = {"SIM:A": [(100.0, 100.0)] * 3 + [(200.0, 200.0)] * (n - 3), "SIM:B": FX.flat(n), "SIM:D": FX.flat(n)}
    d = FX.simple_double(prices, n=n, sectors=SECTORS)
    s = d.sessions("0", "9")
    r = _run(d, FX.ScriptedSource(s, entries={1: [_strat("SIM:A")], 4: [_strat("SIM:B")], 5: [_strat("SIM:D", stop=99.9)]}, role="STRATEGY"))
    notional = {x["instrument_id"]: x["notional"] for x in r["decisions"] if x["action"] == "ENTRY"}
    assert notional["SIM:A"] == 0.005 * 10_000 * 100 / 10 == 500.0
    eq4 = r["rows"][4]["equity"]
    assert eq4 == 9500.0 + 5 * 200.0 and notional["SIM:B"] == 0.005 * eq4 * 100 / 10            # current equity, not the initial
    assert notional["SIM:D"] == 0.10 * r["rows"][5]["equity"]                                     # the 10 % cap binds


def test_K04_invalid_stop_missing_price_purge_and_embargo():
    d = _book_double()
    s = d.sessions("0", "9")
    entries = {2: [_strat("SIM:A", stop=100.0), _strat("SIM:B", stop=None), Signal("SIM:D", raw_close=None, stop_raw=90.0)],
               7: [_strat("SIM:E", hold=3)], 8: [_strat("SIM:F", hold=3)]}
    r = _run(d, FX.ScriptedSource(s, entries=entries, role="STRATEGY"))
    reasons = {(x["session"], x["instrument_id"]): x["reason"] for x in r["decisions"]}
    assert reasons[(s[2], "SIM:A")] == "INVALID_STOP" and reasons[(s[2], "SIM:B")] == "INVALID_STOP" and reasons[(s[2], "SIM:D")] == "NO_DECISION_PRICE"
    assert reasons[(s[7], "SIM:E")] == "SIGNAL" and reasons[(s[8], "SIM:F")] == "PURGE_BOUNDARY"    # 8 + 3 + 1 > 11; 7 + 3 + 1 = 11
    emb = _run(d, FX.ScriptedSource(s, entries={2: [_strat("SIM:A")], 3: [_strat("SIM:B")]}, role="STRATEGY"), embargo_sessions=3)
    r2 = {(x["session"], x["instrument_id"]): x["reason"] for x in emb["decisions"]}
    assert r2 == {(s[2], "SIM:A"): "EMBARGO", (s[3], "SIM:B"): "SIGNAL"}


def test_K05_the_book_is_frozen():
    d = _book_double()
    with pytest.raises(ValueError):
        EN.run(d, FX.ScriptedSource(d.sessions("0", "9"), role="STRATEGY"), FX.v1_config(d, book_overrides={"max_positions": 5}))
    with pytest.raises(TypeError):
        md_eval.BOOK["max_positions"] = 5
    assert dict(md_eval.BOOK) == {"max_positions": 4, "max_new_per_session": 2, "max_per_sector": 2, "risk_fraction_of_current_equity": 0.005,
                                  "max_position_pct_of_current_equity": 10.0, "margin": False}


def test_K06_fill_time_limits_recheck_what_is_actually_held():
    assert BK.fill_limits_ok({"a": "T", "b": "T", "c": "H", "d": "H"}, "E") == "MAX_POSITIONS"
    assert BK.fill_limits_ok({"a": "T", "b": "T"}, "T") == "MAX_PER_SECTOR"
    assert BK.fill_limits_ok({"a": "T"}, "T") is None


def test_K07_under_one_share_is_not_taken():
    d = _book_double()
    s = d.sessions("0", "9")
    r = _run(d, FX.ScriptedSource(s, entries={1: [Signal("SIM:A", notional=50.0)]}))
    assert [(x["instrument_id"], x["reason"]) for x in r["not_taken"]] == [("SIM:A", "SIZE_BELOW_ONE_SHARE")] and not r["fills"]


def test_K08_the_engine_rechecks_positions_and_sector_at_the_fill_when_an_exit_did_not_fill():
    n = 12
    prices = {k: FX.flat(n, missing=(5,) if k == "SIM:A" else ()) for k in SECTORS}
    d = FX.simple_double(prices, n=n, sectors=SECTORS)
    s = d.sessions("0", "9")
    r = _run(d, FX.ScriptedSource(s, entries={1: [_strat("SIM:A"), _strat("SIM:B")], 2: [_strat("SIM:D"), _strat("SIM:E")], 4: [_strat("SIM:F")]},
                                  exits={4: ["SIM:A"]}, role="STRATEGY"))
    assert [(x["instrument_id"], x["reason"], x["phase"]) for x in r["not_taken"]] == [("SIM:A", "NO_FILL_GAP", "FILL"), ("SIM:F", "MAX_POSITIONS", "FILL")]
    assert (s[4], "SIM:F", "ENTRY", "SIGNAL", 500.0) in _dec(r)                                     # accepted at the decision: A was to exit
    prices2 = {k: FX.flat(n, missing=(4,) if k == "SIM:A" else ()) for k in SECTORS}
    d2 = FX.simple_double(prices2, n=n, sectors=SECTORS)
    r2 = _run(d2, FX.ScriptedSource(s, entries={1: [_strat("SIM:A"), _strat("SIM:B")], 3: [_strat("SIM:C")]}, exits={3: ["SIM:A"]}, role="STRATEGY"))
    assert [(x["instrument_id"], x["reason"], x["phase"]) for x in r2["not_taken"]] == [("SIM:A", "NO_FILL_GAP", "FILL"), ("SIM:C", "MAX_PER_SECTOR", "FILL")]


def test_K09_a_static_classification_map_is_flagged_and_its_sha256_is_in_the_identity():
    n = 6
    d = FX.simple_double({"SIM:X": FX.flat(n)}, n=n, sectors={"SIM:X": None})
    s = d.sessions("0", "9")
    smap = {"SIM:X": {"sector": "TECH"}}
    r = _run(d, FX.ScriptedSource(s, entries={1: [_strat("SIM:X")]}, role="STRATEGY"), static_map=smap)
    assert "STATIC_CLASSIFICATION_MAP" in r["flags"] and r["identity"]["static_map_sha256"] == EN.sha256_of(smap)
    plain = _run(d, FX.ScriptedSource(s, entries={1: [_strat("SIM:X")]}, role="STRATEGY"))
    assert "STATIC_CLASSIFICATION_MAP" not in plain["flags"] and plain["identity"]["static_map_sha256"] is None


# ============================================================================================================ costs (F)
def test_F01_sec_section_31_is_dated_and_missing_history_uses_the_maximum_with_a_flag():
    assert CO.sec_rate("2026-05-01")[0] == 20.60 and CO.sec_rate("2026-04-04")[0] == 20.60
    assert CO.sec_rate("2025-06-01")[0] == 0.0 and CO.sec_rate("2026-04-03")[0] == 0.0
    rate, row, missing = CO.sec_rate("2024-01-02")
    assert (rate, row, missing) == (20.60, None, True)


def test_F02_finra_taf_rate_cap_holiday_and_missing_history():
    assert CO.taf("2026-10-12")[:2] == (0.0, 0.0) and "holiday" in CO.taf("2026-10-12")[2]["source"]
    assert CO.taf("2026-03-02")[:2] == (0.000195, 9.79) and CO.taf("2026-03-02")[3] is False
    assert CO.taf("2025-01-02")[:2] == (0.000195, 9.79) and CO.taf("2025-01-02")[3] is True
    m = CO.CostModel()
    top = m.bucket(5e8)[0]
    total, parts, flags = m.cost("SELL", 100_000, 10.0, top, "2026-03-02")
    assert parts["finra_taf"] == 9.79 and parts["sec_section_31"] == 0.0 and math.isclose(parts["friction"], 1e6 * 6e-4)   # Section 31 at 0 until 2026-04-03
    assert math.isclose(m.cost("SELL", 100_000, 10.0, top, "2026-05-01")[1]["sec_section_31"], 20.60)                    # 20.60 per million from 2026-04-04
    assert {"TAF_SOURCE_DISCREPANCY_UNVERIFIED", "CAT_FEE_NOT_MODELLED", "COMMISSION_ASSUMED_UNVERIFIED"} <= set(flags)
    assert "REGULATORY_FEE_HISTORY_MISSING" not in flags and parts["cat"] == 0.0 and math.isclose(total, sum(parts.values()))
    assert m.cost("SELL", 1000, 10.0, top, "2026-03-02")[1]["finra_taf"] == 1000 * 0.000195             # below the cap
    _, bparts, bflags = m.cost("BUY", 100_000, 10.0, top, "2026-03-02")
    assert bparts["sec_section_31"] == bparts["finra_taf"] == 0.0 and "TAF_SOURCE_DISCREPANCY_UNVERIFIED" not in bflags


def test_F03_buckets_are_assumption_defaults_and_unmeasurable_liquidity_is_conservative():
    m = CO.CostModel()
    assert m.bucket(5e8) == (dict(CO.DEFAULT_BUCKETS[0]), ["COST_BUCKETS_ASSUMPTION"])
    assert m.bucket(5e7)[0]["half_spread_bps"] == 2.0 and m.bucket(1e6)[0]["slippage_bps"] == 10.0
    assert m.bucket(None) == (dict(CO.DEFAULT_BUCKETS[-1]), ["COST_BUCKETS_ASSUMPTION", "LIQUIDITY_BUCKET_CONSERVATIVE"])
    assert m.identity()["label"] == "ASSUMPTION" and CO.COST_LABEL == "ASSUMPTION"


def test_F04_the_2x_level_doubles_slippage_and_spread_only_and_other_levels_are_refused():
    one, two = CO.CostModel(multiplier=1.0), CO.CostModel(multiplier=2.0)
    b = one.bucket(5e8)[0]
    p1, p2 = one.cost("SELL", 1000, 50.0, b, "2026-05-01")[1], two.cost("SELL", 1000, 50.0, b, "2026-05-01")[1]
    assert math.isclose(p2["friction"], 2 * p1["friction"]) and p2["sec_section_31"] == p1["sec_section_31"] and p2["finra_taf"] == p1["finra_taf"]
    with pytest.raises(ValueError):
        CO.CostModel(multiplier=3.0)
    assert CO.break_even_bps_per_side(100.0, 1_000_000.0) == {"bps_per_side": 1.0, "label": "FIRST_ORDER"}
    assert CO.break_even_bps_per_side(5.0, 0.0)["bps_per_side"] is None


def test_F05_an_engine_run_with_a_sale_carries_every_cost_flag_and_the_assumption_label():
    d = _book_double()
    s = d.sessions("0", "9")
    r = EN.run(d, FX.ScriptedSource(s, entries={1: [Signal("SIM:A", notional=1000.0)]}, exits={4: ["SIM:A"]}), FX.v1_config(d, initial_cash=10_000.0))
    assert {"COST_BUCKETS_ASSUMPTION", "REGULATORY_FEE_HISTORY_MISSING", "TAF_SOURCE_DISCREPANCY_UNVERIFIED", "CAT_FEE_NOT_MODELLED",
            "COMMISSION_ASSUMED_UNVERIFIED"} <= set(r["flags"])
    sell = [f for f in r["fills"] if f["kind"] == "EXIT"][0]
    assert sell["costs"]["sec_section_31"] == sell["shares"] * sell["price"] * 20.60 / 1e6                # 2024: no recorded rate -> the maximum
    assert r["identity"]["costs"]["label"] == "ASSUMPTION" and r["total_costs"] > 0 and r["final_equity"] < 10_000.0


def test_F06_the_buy_notional_of_a_benchmark_leaves_room_for_its_costs():
    d = _book_double()
    s = d.sessions("0", "9")
    r = EN.run(d, FX.ScriptedSource(s, entries={0: [Signal("SIM:A", weight=1.0)]}), FX.v1_config(d, initial_cash=10_000.0))
    f = r["fills"][0]
    assert f["shares"] == 99 and f["shares"] * f["price"] + f["cost"] <= 10_000.0 and r["rows"][1]["cash"] >= 0.0


# ============================================================================================================ the cash rate (R)
def test_R01_zero_base_case_and_a_hypothetical_constant_is_labelled():
    d = _book_double(n=6)
    base = _run(d, FX.ScriptedSource(d.sessions("0", "9")))
    assert base["final_equity"] == 10_000.0 and base["identity"]["cash_rate"]["label"] == "ZERO_BASE_CASE" and "CASH_RATE_HYPOTHETICAL" not in base["flags"]
    hyp = _run(d, FX.ScriptedSource(d.sessions("0", "9")), cash_rate=CR.hypothetical(0.05))
    daily = 1.05 ** (1 / 252) - 1
    assert math.isclose(hyp["final_equity"], 10_000.0 * (1 + daily) ** 6, rel_tol=1e-12)
    assert hyp["identity"]["cash_rate"]["label"] == "HYPOTHETICAL" and "CASH_RATE_HYPOTHETICAL" in hyp["flags"]


def test_R02_a_series_not_registered_by_sha_is_refused():
    import hashlib
    import json
    series = {"2024-01-02": 0.0001}
    sha = hashlib.sha256(json.dumps(series, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()
    assert CR.REGISTERED_SERIES_SHA256 == frozenset()
    with pytest.raises(CR.CashRateRefused):
        CR.registered_series(series, sha)
    with pytest.raises(CR.CashRateRefused):
        CR.registered_series(series, "0" * 64)


def test_R03_the_cash_rate_constructor_itself_refuses_a_mismatched_or_unregistered_series(monkeypatch):
    series = {"2024-01-02": 0.0001}
    sha = CR.series_sha256(series)
    for kw in ({"series": series, "sha256": sha}, {"series": series, "sha256": "0" * 64}, {"series": {}, "sha256": sha}):
        with pytest.raises(CR.CashRateRefused):
            CR.CashRate("REGISTERED_SERIES", **kw)                                          # LOW-2: no direct construction skips the checks
    with pytest.raises(CR.CashRateRefused):
        CR.CashRate("T_BILLS")
    monkeypatch.setattr(CR, "REGISTERED_SERIES_SHA256", frozenset({sha}))                    # positive control: a registered, matching series
    assert CR.CashRate("REGISTERED_SERIES", series=series, sha256=sha).daily("2024-01-02") == 0.0001
    with pytest.raises(CR.CashRateRefused):
        CR.CashRate("REGISTERED_SERIES", series={"2024-01-02": 0.0002}, sha256=sha)          # registered sha, but another series
