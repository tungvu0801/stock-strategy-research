"""
MD-B (md_eval): the contract's ledger set (base §7 (e), (f), (g), (h); P3a; amendment A3, A5), hand-computed on synthetic fixtures with zero
costs, each with a control that must fail without the rule.
  L01-L03  (e) split / stock dividend: shares x ratio, total basis kept, cash unchanged; cash in lieu or FRACTION_UNPAID; applied BEFORE that
           session's fills (a sell and a buy at the split open); A3: carried stop / target divided by the ratio (control: a ledger that keeps them)
  L04-L05  (f) cash dividend: credited once on pay_session; buy at the ex-date open not entitled, sell at the ex-date open entitled; not also in
           the price return (the final equity is exact); PAY_DATE_UNKNOWN credited on the ex-date
  L06-L09  (g) spin-off (basis by raw closes), an unaccounted child, merger (stock + cash), delisting (UNKNOWN -> -100 % primary, last price as the
           labelled sensitivity, a known return), booked on the session after last_trade_session; CORPORATE_ACTION_UNACCOUNTED when the events are
           incomplete and something is held
  L10-L11  (h) reconciliation on the pinned exact fixture: ledger return == total-return return within 1e-9 relative; control: a ledger that
           credits the dividend twice fails it
  L12-L14  A5: STALE_MARK valuation, POSSIBLE_DELISTING, SIZING_FROM_STALE_EQUITY; cash never below 0 (CASH, the ledger's own refusal);
           no total-return factor in the ledger
"""
from pathlib import Path

import pytest

import md_eval_fixtures as FX
from md_eval import engine as EN
from md_eval import ledger as LG
from md_eval.benchmarks import total_return_series
from md_eval.signals import BuyAndHold, Signal
import research_gate as RGATE

ROOT = Path(__file__).resolve().parents[1]


def _run(d, src, **kw):
    kw.setdefault("cost_model", FX.zero_costs())
    kw.setdefault("initial_cash", 10_000.0)
    return EN.run(d, src, FX.v1_config(d, **kw))


def _pos(r, iid):
    return [p for p in r["open_positions"] if p["instrument_id"] == iid]


# ============================================================================================================ (e) splits
def _split_fixture():
    n = 10
    pre_post = lambda a, b: [(a, a) if k < 5 else (b, b) for k in range(n)]  # noqa: E731
    prices = {"SIM:X": pre_post(100.0, 50.0), "SIM:Y": pre_post(90.0, 60.0), "SIM:Z": pre_post(90.0, 60.0), "SIM:W": pre_post(100.0, 50.0),
              "SIM:V": pre_post(100.0, 50.0)}
    s = FX.simple_double({"SIM:X": FX.flat(n)}, n=n).sessions("0", "9")
    events = [FX.ev("S-X", "SIM:X", "SPLIT", s, 5, ratio=2.0), FX.ev("S-W", "SIM:W", "SPLIT", s, 5, ratio=2.0),
              FX.ev("S-V", "SIM:V", "SPLIT", s, 5, ratio=2.0), FX.ev("SD-Y", "SIM:Y", "STOCK_DIVIDEND", s, 5, ratio=1.5, cash_in_lieu_per_share=60.0),
              FX.ev("SD-Z", "SIM:Z", "STOCK_DIVIDEND", s, 5, ratio=1.5)]
    return FX.simple_double(prices, n=n, events=events), s


def test_L01_split_and_stock_dividend_shares_basis_cash_in_lieu_and_fraction_unpaid():
    d, s = _split_fixture()
    src = FX.ScriptedSource(s, entries={1: [Signal("SIM:X", notional=300.0), Signal("SIM:Y", notional=270.0), Signal("SIM:Z", notional=270.0),
                                            Signal("SIM:W", notional=200.0)], 4: [Signal("SIM:V", notional=100.0)]}, exits={4: ["SIM:W"]})
    r = _run(d, src)
    assert _pos(r, "SIM:X")[0]["shares"] == 6 and _pos(r, "SIM:X")[0]["cost_basis"] == 300.0
    assert _pos(r, "SIM:Y")[0]["shares"] == 4 and _pos(r, "SIM:Y")[0]["cost_basis"] == 270.0
    assert _pos(r, "SIM:Z")[0]["shares"] == 4 and "FRACTION_UNPAID" in r["flags"]
    logs = {e["event_id"]: e for e in r["ledger_log"] if e.get("kind") in ("SPLIT", "STOCK_DIVIDEND")}
    assert logs["S-X"]["cash_in_lieu"] == 0.0 and logs["SD-Y"]["cash_in_lieu"] == 30.0 and logs["SD-Z"]["cash_in_lieu"] == 0.0
    assert [e for e in r["ledger_log"] if e.get("flag") == "FRACTION_UNPAID"][0]["instrument_id"] == "SIM:Z"
    row5 = r["rows"][5]
    assert row5["cash"] == 10_000.0 - 300 - 270 - 270 - 200 + 30 + 200 - 100 == 9090.0
    assert row5["equity"] == 9970.0                                                       # Z's unpaid half share (30 USD) is the only loss


def test_L02_the_split_is_applied_before_that_sessions_fills():
    d, s = _split_fixture()
    src = FX.ScriptedSource(s, entries={1: [Signal("SIM:W", notional=200.0)], 4: [Signal("SIM:V", notional=100.0)]}, exits={4: ["SIM:W"]})
    r = _run(d, src)
    w = [f for f in r["fills"] if f["instrument_id"] == "SIM:W" and f["kind"] == "EXIT"][0]
    assert (w["session"], w["shares"], w["price"]) == (s[5], 4, 50.0)                    # 2 shares became 4 before the open's sell
    v = [f for f in r["fills"] if f["instrument_id"] == "SIM:V"][0]
    assert (v["session"], v["shares"]) == (s[5], 2) and _pos(r, "SIM:V")[0]["shares"] == 2   # a buy at the split open is not split again


def test_L03_a3_carried_stop_and_target_are_divided_by_the_ratio_and_the_control_fails(monkeypatch):
    n = 10
    d = FX.simple_double({"SIM:X": [(100.0, 100.0) if k < 5 else (50.0, 50.0) for k in range(n)]}, n=n,
                         events=[FX.ev("S-X", "SIM:X", "SPLIT", FX.simple_double({"SIM:X": FX.flat(n)}, n=n).sessions("0", "9"), 5, ratio=2.0)])
    s = d.sessions("0", "9")
    src = lambda: FX.ScriptedSource(s, role="STRATEGY", entries={1: [Signal("SIM:X", raw_close=100.0, stop_raw=90.0, target_raw=300.0)]})  # noqa: E731
    r = _run(d, src())
    p = _pos(r, "SIM:X")[0]
    assert (p["shares"], p["stop_raw"], p["target_raw"]) == (10, 45.0, 150.0) and not [x for x in r["decisions"] if x["action"] == "EXIT"]
    orig = LG.Ledger.split

    def keeps_levels(self, ev, session):
        pos = self.positions.get(ev.instrument_id)
        saved = (pos.stop_raw, pos.target_raw) if pos else None
        orig(self, ev, session)
        if pos and saved:
            pos.stop_raw, pos.target_raw = saved
    monkeypatch.setattr(LG.Ledger, "split", keeps_levels)
    bad = _run(d, src())
    assert [x["reason"] for x in bad["decisions"] if x["action"] == "EXIT"] == ["STOP"]  # control: a spurious stop at the split


# ============================================================================================================ (f) cash dividends
def _div_fixture(pay=True):
    n = 12
    s = FX.simple_double({"SIM:A": FX.flat(n)}, n=n).sessions("0", "9")
    px = [(100.0, 100.0) if k < 6 else (98.0, 98.0) for k in range(n)]
    kw = {"pay_session": s[9]} if pay else {}
    events = [FX.ev(f"D-{x}", f"SIM:{x}", "CASH_DIVIDEND", s, 6, known_k=2, cash_amount=2.0, **kw) for x in "ABC"]
    return FX.simple_double({"SIM:A": px, "SIM:B": list(px), "SIM:C": list(px)}, n=n, events=events), s


def test_L04_dividend_entitlement_by_the_ex_date_rule_credited_once_on_pay_session():
    d, s = _div_fixture()
    src = FX.ScriptedSource(s, entries={1: [Signal("SIM:A", notional=1000.0), Signal("SIM:C", notional=1000.0)], 5: [Signal("SIM:B", notional=1000.0)]},
                            exits={5: ["SIM:C"]})
    r = _run(d, src)
    credits = [e for e in r["ledger_log"] if e["kind"] == "DIVIDEND_CREDITED"]
    assert sorted((e["instrument_id"], e["session"], e["amount"]) for e in credits) == [("SIM:A", s[9], 20.0), ("SIM:C", s[9], 20.0)]
    assert r["dividends_credited"] == 40.0 and not r["receivables"]
    assert r["rows"][8]["cash"] == 8000.0 and r["rows"][9]["cash"] == 8040.0
    assert r["final_equity"] == 10_000.0                                                    # the dividend is not also in the price return
    assert "PAY_DATE_UNKNOWN" not in r["flags"]


def test_L05_an_unknown_pay_session_is_credited_on_the_ex_date_and_flagged():
    d, s = _div_fixture(pay=False)
    r = _run(d, FX.ScriptedSource(s, entries={1: [Signal("SIM:A", notional=1000.0)]}))
    credits = [e for e in r["ledger_log"] if e["kind"] == "DIVIDEND_CREDITED"]
    assert [(e["session"], e["amount"]) for e in credits] == [(s[6], 20.0)] and "PAY_DATE_UNKNOWN" in r["flags"]
    assert r["rows"][6]["cash"] == 9000.0 + 20.0


# ============================================================================================================ (g) spin-off, merger, delisting
def test_L06_spin_off_basis_by_raw_closes_and_the_received_child_is_sold_at_the_next_open():
    n = 10
    prices = {"SIM:P": [(100.0, 100.0) if k < 5 else (80.0, 80.0) for k in range(n)], "SIM:K": [None] * 5 + [(40.0, 40.0)] * 5}
    s = FX.simple_double({"SIM:P": FX.flat(n)}, n=n).sessions("0", "9")
    d = FX.simple_double(prices, n=n, events=[FX.ev("SO", "SIM:P", "SPIN_OFF", s, 5, child_instrument_id="SIM:K", child_ratio=0.5)])
    r = _run(d, FX.ScriptedSource(s, entries={1: [Signal("SIM:P", notional=1000.0)]}))
    so = [e for e in r["ledger_log"] if e["kind"] == "SPIN_OFF"][0]
    assert (so["child_shares"], so["basis_rule"], so["child_basis_share"]) == (5, "RAW_CLOSES_ON_EFFECTIVE_SESSION", 0.2)
    assert _pos(r, "SIM:P")[0]["cost_basis"] == 800.0
    k = [f for f in r["fills"] if f["instrument_id"] == "SIM:K"][0]
    assert (k["session"], k["kind"], k["shares"], k["price"], k["reason"]) == (s[6], "EXIT", 5, 40.0, "RECEIVED_SHARES")
    assert r["final_equity"] == 9000.0 + 10 * 80.0 + 200.0
    nochild = FX.simple_double({"SIM:P": prices["SIM:P"], "SIM:K": [None] * n}, n=n,
                               events=[FX.ev("SO", "SIM:P", "SPIN_OFF", s, 5, child_instrument_id="SIM:K", child_ratio=0.5)])
    r2 = _run(nochild, FX.ScriptedSource(s, entries={1: [Signal("SIM:P", notional=1000.0)]}))
    assert "CORPORATE_ACTION_UNACCOUNTED" in r2["flags"]


def test_L07_merger_books_acquirer_shares_and_cash():
    n = 10
    prices = {"SIM:T": [(100.0, 100.0)] * 5 + [None] * 5, "SIM:Q": [(200.0, 200.0)] * n}
    s = FX.simple_double({"SIM:T": FX.flat(n)}, n=n).sessions("0", "9")
    d = FX.simple_double(prices, n=n, events=[FX.ev("M", "SIM:T", "MERGER", s, 5, acquirer_instrument_id="SIM:Q", stock_ratio=0.5, cash_amount=10.0)])
    r = _run(d, FX.ScriptedSource(s, entries={1: [Signal("SIM:T", notional=1000.0)]}))
    m = [e for e in r["ledger_log"] if e["kind"] == "MERGER"][0]
    assert (m["session"], m["acquirer_shares"], m["cash"]) == (s[5], 5, 100.0)
    q = [f for f in r["fills"] if f["instrument_id"] == "SIM:Q"][0]
    assert (q["session"], q["shares"], q["price"]) == (s[6], 5, 200.0) and r["final_equity"] == 10_100.0


@pytest.mark.parametrize("mode,ret,proceeds,flag", [("PRIMARY", None, 0.0, None), ("LAST_PRICE", None, 500.0, "DELISTING_LAST_PRICE_SENSITIVITY"),
                                                    ("PRIMARY", -0.3, 350.0, None)])
def test_L08_delisting_unknown_return_is_minus_100_percent_primary_and_last_price_is_the_labelled_sensitivity(mode, ret, proceeds, flag):
    n = 10
    s = FX.simple_double({"SIM:D": FX.flat(n)}, n=n).sessions("0", "9")
    kw = {} if ret is None else {"delisting_return": ret}
    d = FX.simple_double({"SIM:D": [(50.0, 50.0)] * 5 + [None] * 5, "SIM:E": FX.flat(n)}, n=n,
                         events=[FX.ev("DL", "SIM:D", "DELISTING", s, 7, last_trade_session=s[4], last_raw_price=50.0, **kw)])
    r = _run(d, FX.ScriptedSource(s, entries={1: [Signal("SIM:D", notional=500.0)]}), delisting_mode=mode)
    dl = [e for e in r["ledger_log"] if e["kind"] == "DELISTING"][0]
    assert dl["session"] == s[5] and abs(dl["proceeds"] - proceeds) <= 1e-9                 # the session after last_trade_session (earlier than 7)
    assert abs(r["final_equity"] - (9500.0 + proceeds)) <= 1e-9 and not r["open_positions"]
    assert ("DELISTING_RETURN_UNKNOWN" in r["flags"]) is (ret is None)
    assert (flag in r["flags"]) if flag else "DELISTING_LAST_PRICE_SENSITIVITY" not in r["flags"]
    assert "POSSIBLE_DELISTING" not in r["flags"] and "STALE_MARK" not in r["flags"]


def test_L09_incomplete_events_flag_every_held_instrument_and_nothing_when_nothing_is_held():
    n = 8
    d = FX.simple_double({"SIM:X": FX.flat(n)}, n=n, completeness="NONE")
    s = d.sessions("0", "9")
    held = _run(d, FX.ScriptedSource(s, entries={1: [Signal("SIM:X", notional=1000.0)]}))
    assert "CORPORATE_ACTION_UNACCOUNTED" in held["flags"]
    assert [e["instrument_id"] for e in held["ledger_log"] if e.get("flag") == "CORPORATE_ACTION_UNACCOUNTED"] == ["SIM:X"]
    idle = _run(d, FX.ScriptedSource(s))
    assert "CORPORATE_ACTION_UNACCOUNTED" not in idle["flags"]


# ============================================================================================================ (h) reconciliation
def _recon_fixture():
    closes = [100.0, 100.0, 110.0, 55.0, 60.0, 58.0, 58.0, 58.0]
    n = len(closes)
    prices = {"SIM:S": [(c, c) for c in closes]}
    factors = {"SIM:S": [(2.0, 2.0 * 60.0 / 58.0) if k < 3 else ((1.0, 60.0 / 58.0) if k < 5 else (1.0, 1.0)) for k in range(n)]}
    s = FX.simple_double({"SIM:S": FX.flat(n)}, n=n).sessions("0", "9")
    events = [FX.ev("SPL", "SIM:S", "SPLIT", s, 3, ratio=2.0), FX.ev("DIV", "SIM:S", "CASH_DIVIDEND", s, 5, cash_amount=2.0, pay_session=s[5])]
    return FX.simple_double(prices, n=n, events=events, factors=factors), s


def _recon(d, s):
    r = _run(d, BuyAndHold("SIM:S"))
    assert r["fills"][0]["shares"] == 100 and r["rows"][1]["cash"] == 0.0                    # the notional is an exact multiple of raw_open
    ledger_growth = r["rows"][-1]["equity"] / r["rows"][1]["equity"]
    tr = total_return_series(RGATE.admit_double(d), "SIM:S", s)                              # I-1B-1 (O-7): the benchmark reads an admitted dataset
    tr_growth = 1.0
    for x in tr[2:]:
        tr_growth *= 1.0 + x
    return ledger_growth, tr_growth


def test_L10_reconciliation_on_the_pinned_exact_fixture_within_1e_9():
    d, s = _recon_fixture()
    lg, tg = _recon(d, s)
    assert abs(lg / tg - 1.0) <= 1e-9 and abs(lg - 1.2) <= 1e-12


def test_L11_control_a_ledger_that_counts_the_dividend_twice_fails_the_reconciliation(monkeypatch):
    d, s = _recon_fixture()
    orig = LG.Ledger._credit
    monkeypatch.setattr(LG.Ledger, "_credit", lambda self, r, session: (orig(self, r, session), orig(self, r, session)))
    lg, tg = _recon(d, s)
    assert abs(lg / tg - 1.0) > 1e-9


# ============================================================================================================ the fix round (LOW-2, LOW-7, LOW-5)
def test_L15_a_same_session_split_and_cash_dividend_credits_the_old_share_count():
    n = 10
    s = FX.simple_double({"SIM:X": FX.flat(n)}, n=n).sessions("0", "9")
    events = [FX.ev("A-SPLIT", "SIM:X", "SPLIT", s, 5, ratio=2.0), FX.ev("B-DIV", "SIM:X", "CASH_DIVIDEND", s, 5, cash_amount=1.0, pay_session=s[5])]
    d = FX.simple_double({"SIM:X": [(100.0, 100.0)] * 5 + [(49.5, 49.5)] * 5}, n=n, events=events)    # A-SPLIT sorts first by event_id
    r = _run(d, FX.ScriptedSource(s, entries={1: [Signal("SIM:X", notional=1000.0)]}))
    credit = [e for e in r["ledger_log"] if e["kind"] == "DIVIDEND_CREDITED"]
    assert [(c["amount"], c["entitled_shares"]) for c in credit] == [(10.0, 10)]                 # old_shares x D, not 20 x D
    assert _pos(r, "SIM:X")[0]["shares"] == 20


def _merger(acquirer_bars, n=10):
    s = FX.simple_double({"SIM:T": FX.flat(n)}, n=n).sessions("0", "9")
    d = FX.simple_double({"SIM:T": [(100.0, 100.0)] * 5 + [None] * 5, "SIM:Q": acquirer_bars}, n=n,
                         events=[FX.ev("M", "SIM:T", "MERGER", s, 5, acquirer_instrument_id="SIM:Q", stock_ratio=0.5, cash_amount=10.0)])
    r = _run(d, FX.ScriptedSource(s, entries={1: [Signal("SIM:T", notional=1000.0)]}))
    return [e for e in r["ledger_log"] if e["kind"] == "MERGER"][0], r, s


def test_L16_merger_basis_is_allocated_pro_rata_by_value_with_the_rule_recorded():
    m, r, s = _merger([(200.0, 200.0)] * 10)
    assert m["basis_rule"] == "PRO_RATA_BY_VALUE_ON_EFFECTIVE_SESSION" and abs(m["acquirer_basis"] - 1000.0 * 1000.0 / 1100.0) <= 1e-9
    assert abs(m["realised_on_cash"] - (100.0 - (1000.0 - 1000.0 * 1000.0 / 1100.0))) <= 1e-9
    later, _, s2 = _merger([None] * 6 + [(200.0, 200.0)] * 4)
    assert later["basis_rule"] == f"PRO_RATA_BY_VALUE_ACQUIRER_FIRST_RAW_CLOSE_AT_OR_AFTER:{s2[6]}" and abs(later["acquirer_basis"] - 1000.0 * 1000.0 / 1100.0) <= 1e-9


def test_L17_an_acquirer_with_no_close_at_or_after_the_effective_session_is_unaccounted():
    m, r, _ = _merger([None] * 10)
    assert m["basis_rule"] == "UNACCOUNTED_BASIS_TO_ACQUIRER" and "CORPORATE_ACTION_UNACCOUNTED" in r["flags"]
    assert [e["why"] for e in r["ledger_log"] if e.get("flag") == "CORPORATE_ACTION_UNACCOUNTED"] == ["ACQUIRER_HAS_NO_CLOSE_AT_OR_AFTER"]


def test_L18_spin_off_cost_basis_allocation_and_the_child_first_close_rule():
    n = 10
    s = FX.simple_double({"SIM:P": FX.flat(n)}, n=n).sessions("0", "9")
    parent = [(100.0, 100.0) if k < 5 else (80.0, 80.0) for k in range(n)]
    given = FX.simple_double({"SIM:P": parent, "SIM:K": [None] * 5 + [(40.0, 40.0)] * 5}, n=n,
                             events=[FX.ev("SO", "SIM:P", "SPIN_OFF", s, 5, child_instrument_id="SIM:K", child_ratio=0.5, cost_basis_allocation=0.3)])
    r = _run(given, FX.ScriptedSource(s, entries={1: [Signal("SIM:P", notional=1000.0)]}))
    so = [e for e in r["ledger_log"] if e["kind"] == "SPIN_OFF"][0]
    assert (so["basis_rule"], so["child_basis_share"]) == ("COST_BASIS_ALLOCATION", 0.3) and abs(_pos(r, "SIM:P")[0]["cost_basis"] - 700.0) <= 1e-9
    late = FX.simple_double({"SIM:P": parent, "SIM:K": [None] * 6 + [(40.0, 40.0)] * 4}, n=n,
                            events=[FX.ev("SO", "SIM:P", "SPIN_OFF", s, 5, child_instrument_id="SIM:K", child_ratio=0.5)])
    r2 = _run(late, FX.ScriptedSource(s, entries={1: [Signal("SIM:P", notional=1000.0)]}))
    so2 = [e for e in r2["ledger_log"] if e["kind"] == "SPIN_OFF"][0]
    assert so2["basis_rule"] == f"CHILD_FIRST_RAW_CLOSE_AT_OR_AFTER:{s[6]}" and so2["child_basis_share"] == 0.2
    assert "STALE_MARK" in r2["flags"]                                                     # never valued at the later close on the effective session


def test_L19_an_exit_whose_position_was_delisted_at_the_open_is_position_gone():
    n = 10
    s = FX.simple_double({"SIM:D": FX.flat(n)}, n=n).sessions("0", "9")
    d = FX.simple_double({"SIM:D": [(50.0, 50.0)] * 5 + [None] * 5}, n=n,
                         events=[FX.ev("DL", "SIM:D", "DELISTING", s, 5, last_trade_session=s[4], last_raw_price=50.0, delisting_return=0.0)])
    r = _run(d, FX.ScriptedSource(s, entries={1: [Signal("SIM:D", notional=500.0)]}, exits={4: ["SIM:D"]}))
    assert [(x["instrument_id"], x["session"], x["reason"], x["phase"]) for x in r["not_taken"]] == [("SIM:D", s[5], "POSITION_GONE", "FILL")]


@pytest.mark.parametrize("typ,ratio,post", [("SPLIT", 2.0, 50.0), ("REVERSE_SPLIT", 0.1, 1000.0)])
def test_L20_a_split_on_the_fill_session_rescales_a_pending_entrys_stop_and_target(monkeypatch, typ, ratio, post):
    n = 8
    s = FX.simple_double({"SIM:X": FX.flat(n)}, n=n).sessions("0", "9")
    d = FX.simple_double({"SIM:X": [(100.0, 100.0)] * 3 + [(post, post)] * 5}, n=n, events=[FX.ev("S", "SIM:X", typ, s, 3, ratio=ratio)])
    src = lambda: FX.ScriptedSource(s, role="STRATEGY", entries={2: [Signal("SIM:X", raw_close=100.0, stop_raw=99.0, target_raw=130.0)]})  # noqa: E731
    r = _run(d, src())
    p = _pos(r, "SIM:X")[0]
    assert (p["stop_raw"], p["target_raw"]) == (99.0 / ratio, 130.0 / ratio) and not [x for x in r["decisions"] if x["action"] == "EXIT"]
    assert [(e["before"], e["after"]) for e in r["ledger_log"] if e["kind"] == "PENDING_LEVELS_RESCALED"] == [([99.0, 130.0], [99.0 / ratio, 130.0 / ratio])]
    monkeypatch.setattr(EN._Run, "_rescale_pending_entries", lambda self, e, d: None)        # control: the unpatched behaviour
    bad = _run(d, src())
    assert [x["reason"] for x in bad["decisions"] if x["action"] == "EXIT"] == ["STOP" if ratio > 1 else "TARGET"]   # the spurious exit


def test_L21_an_unknown_ratio_refuses_the_pending_entry_instead_of_filling_unconverted_levels():
    n = 8
    s = FX.simple_double({"SIM:X": FX.flat(n)}, n=n).sessions("0", "9")
    d = FX.simple_double({"SIM:X": [(100.0, 100.0)] * 3 + [(50.0, 50.0)] * 5}, n=n, events=[FX.ev("S", "SIM:X", "SPLIT", s, 3)])
    r = _run(d, FX.ScriptedSource(s, role="STRATEGY", entries={2: [Signal("SIM:X", raw_close=100.0, stop_raw=99.0, target_raw=130.0)]}))
    assert [(x["instrument_id"], x["session"], x["reason"], x["phase"]) for x in r["not_taken"]] == [
        ("SIM:X", s[3], "SPLIT_RATIO_UNKNOWN_LEVELS_UNCONVERTED", "FILL")]
    assert not r["fills"] and "CORPORATE_ACTION_UNACCOUNTED" in r["flags"]
    assert [e["why"] for e in r["ledger_log"] if e.get("flag") == "CORPORATE_ACTION_UNACCOUNTED"] == ["RATIO_UNKNOWN_PENDING_ENTRY"]


# ============================================================================================================ A5, cash, no total return
def test_L12_stale_mark_possible_delisting_and_sizing_from_stale_equity():
    n = 10
    d = FX.simple_double({"SIM:G": [(100.0, 100.0)] * 6 + [None] * 4, "SIM:H": FX.flat(n)}, n=n)
    s = d.sessions("0", "9")
    r = _run(d, FX.ScriptedSource(s, entries={1: [Signal("SIM:G", notional=1000.0)], 7: [Signal("SIM:H", notional=1000.0)]}))
    assert {"STALE_MARK", "POSSIBLE_DELISTING", "SIZING_FROM_STALE_EQUITY"} <= set(r["flags"])
    stale = [e for e in r["ledger_log"] if e["kind"] == "STALE_MARK"]
    assert [e["session"] for e in stale] == s[6:] and all(e["mark"] == 100.0 and e["mark_session"] == s[5] for e in stale)
    assert all(row["equity"] == 10_000.0 for row in r["rows"]) and r["rows"][7]["stale"] is True


def test_L13_cash_never_goes_below_zero():
    n = 6
    d = FX.simple_double({"SIM:X": FX.flat(n)}, n=n)
    s = d.sessions("0", "9")
    r = _run(d, FX.ScriptedSource(s, entries={1: [Signal("SIM:X", notional=20_000.0)]}))
    assert [(x["instrument_id"], x["reason"]) for x in r["not_taken"]] == [("SIM:X", "CASH")] and all(row["cash"] == 10_000.0 for row in r["rows"])
    led = LG.Ledger(cash=100.0)
    with pytest.raises(LG.LedgerInvariantError):
        led.buy("SIM:X", 2, 60.0, 0.0, s[1], sector="S")
    with pytest.raises(LG.LedgerInvariantError):
        led.buy("SIM:X", 0, 60.0, 0.0, s[1], sector="S")
    assert led.cash == 100.0 and not led.positions


def test_L14_the_ledger_never_reads_a_total_return_factor():
    for rel in ("ledger.py", "engine.py", "book.py"):
        assert "total_return" not in (ROOT / "md_eval" / rel).read_text(encoding="utf-8"), rel   # no total-return factor reaches the ledger
