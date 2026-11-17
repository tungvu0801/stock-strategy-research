"""
MD-B (md_eval): the contract's look-ahead set (base §7 (a), (b), (c), (d), (i); amendment A1, A2, A6, A11, A12), each with a negative control
that must FAIL the property (a deliberately leaky source or double), so the check is shown to detect what it guards.
  C01-C04  contract identity: both contract files' sha256 (A12, the repository's copies), SUPPORTED_CONTRACTS = v2, any other version refused; md_eval's pins = historical_data's
  A01-A02  (a) future perturbation: every value with known_at > t changed -> no decision at or before t changes; control LeakySource
  B01-B02  (b) truncation at t -> the decisions up to t are identical; the view's manifest is exactly the A11 allow-list; control EndAwareSource
  E01-E03  (c) event timing: events, membership changes and scheduled events invisible before known_at (UNKNOWN: before the effective
           session's open); decision level, with the leak_known_at double as the control
  D01-D05  (d) fill timing: fills only at the next calendar session's open; NO_FILL_GAP; the channel refuses any other session and any other
           phase; the decision context reaches no provider (control: view_holds_provider)
  I01-I03  (i) snapshot invariance: rebased factors (last visible = 1.0 exactly); powers of two exact, others within 1e-12; control AbsoluteLevelSource
"""
import gc
import hashlib
import types
from pathlib import Path

import pytest

import md_eval
import md_eval_fixtures as FX
from md_eval import engine as EN
from md_eval.contract import (EARNINGS_DATA_UNAVAILABLE, VIEW_MANIFEST_FIELDS, ExecutionChannelViolation, ScheduledEvent, UnsupportedContract,
                              check_version, is_missing, unknown)
from md_eval.double import ContractDouble
from md_eval.signals import Signal, SignalSource, SwingBridge

CONTRACT_DIR = Path(__file__).resolve().parents[1] / "historical_data" / "contract"   # S3 (I-1A section 7.3): the committed copies (MD-A, MDA-FR-6)


# ============================================================================================================ contract identity (C)
def test_C01_both_contract_files_have_the_pinned_sha256():
    base, amend = CONTRACT_DIR / "HISTORICAL_DATA_CONTRACT_v1.md", CONTRACT_DIR / "HISTORICAL_DATA_CONTRACT_v2_AMENDMENT.md"   # no skip: a missing copy fails
    assert hashlib.sha256(base.read_bytes()).hexdigest() == md_eval.CONTRACT_V1_SHA256 == "453676fd44554ac9f66ce2d168318893ea18d93916ea2eef3ffa0f8c4de40fd7"
    assert hashlib.sha256(amend.read_bytes()).hexdigest() == md_eval.CONTRACT_V2_SHA256 == "7850580ba22195e92ee03f71e75d2d7eb3719176343e90f485ce47c3ac876ed8"
    assert md_eval.CONTRACT_V1_SHA256 in amend.read_text(encoding="utf-8")                  # v2 pins its base by sha


def test_C02_only_contract_v2_with_its_sha_is_supported():
    assert dict(md_eval.SUPPORTED_CONTRACTS) == {"historical_data_contract_v2": md_eval.CONTRACT_V2_SHA256}
    assert md_eval.CONTRACT_BASE_OF["historical_data_contract_v2"] == ("historical_data_contract_v1", md_eval.CONTRACT_V1_SHA256)
    d = FX.v1_double()
    check_version(d.manifest())
    for version, sha in (("historical_data_contract_v1", md_eval.CONTRACT_V1_SHA256), ("historical_data_contract_v2", "0" * 64),
                         ("historical_data_contract_v3", md_eval.CONTRACT_V2_SHA256)):
        with pytest.raises(UnsupportedContract):
            check_version(FX.manifest(["2024-01-01"], 1, version=version, sha=sha))


def test_C03_the_engine_refuses_an_unsupported_contract_before_reading_data():
    s = FX.flat(5)
    d = FX.simple_double({"SIM:X": s}, n=5)
    bad = d.with_defect(manifest=FX.manifest(d.sessions("0", "9"), 1, version="historical_data_contract_v1", sha=md_eval.CONTRACT_V1_SHA256))
    with pytest.raises(UnsupportedContract):
        EN.run(bad, FX.ScriptedSource(bad.sessions("0", "9")), FX.v1_config(bad))
    assert bad.calls == ["manifest"]


def test_C04_md_evals_contract_pins_equal_historical_datas_and_the_identifiers_are_the_id10_strings():
    import historical_data as HD
    assert md_eval.CONTRACT_V1_SHA256 == HD.CONTRACT_BASE_SHA256
    assert md_eval.CONTRACT_V2_SHA256 == HD.CONTRACT_SHA256
    assert (md_eval.CONTRACT_V1, md_eval.CONTRACT_V2) == ("historical_data_contract_v1", "historical_data_contract_v2")   # the exact strings ID-10 exempts


# ============================================================================================================ helpers
def _run(d, src, **kw):
    return EN.run(d, src, FX.v1_config(d, **kw))


def _cmp(a, b):
    return [(x["session"], x["action"], x["instrument_id"], x["reason"], x.get("notional"), x.get("stop_raw"), x.get("target_raw")) for x in a] == \
        [(x["session"], x["action"], x["instrument_id"], x["reason"], x.get("notional"), x.get("stop_raw"), x.get("target_raw")) for x in b]


class LeakySource(SignalSource):
    """NEGATIVE CONTROL: reads the smuggled provider's NEXT bar (look-ahead) and enters when it is 20 % higher."""
    role = "STRATEGY"

    def __init__(self, provider, iid):
        self.p, self.iid = provider, iid

    def entries(self, ctx):
        nxt = ctx.view.next_session(ctx.session)
        today, tomorrow = self.p.bars(self.iid, ctx.session, ctx.session), self.p.bars(self.iid, nxt, nxt)
        if today and tomorrow and tomorrow[0].raw_close > 1.2 * today[0].raw_close:
            c = today[0].raw_close
            return [Signal(self.iid, raw_close=c, stop_raw=0.9 * c, max_hold=3)]
        return []


class EndAwareSource(SignalSource):
    """NEGATIVE CONTROL: uses the dataset's end (from the smuggled full manifest): enters in the last 30 sessions before it."""
    role = "STRATEGY"

    def __init__(self, provider, iid):
        self.p, self.iid = provider, iid

    def entries(self, ctx):
        end = self.p.manifest().end_session
        ss = ctx.view.sessions(ctx.session, end)
        bar = ctx.view.bars(self.iid, ctx.session, ctx.session)
        if bar and len(ss) <= 30:
            c = bar[0].raw_close
            return [Signal(self.iid, raw_close=c, stop_raw=0.9 * c, max_hold=2)]
        return []


# ============================================================================================================ (a) future perturbation
@pytest.mark.parametrize("use_pullback_rule", [True, False])
def test_A01_future_perturbation_changes_no_decision_at_or_before_t(use_pullback_rule):
    d = FX.v1_double()
    s = d.sessions("0", "9")
    base = _run(d, SwingBridge(use_pullback_rule, FX.UNIVERSE))
    assert any(x["action"] == "ENTRY" for x in FX.decisions_until(base, s[230])), "the fixture must produce entries before the last cut"
    assert any(x["action"] == "EXIT" for x in base["decisions"]) and base["fills"]
    reached_later = []
    for t in (205, 218, 230):
        cut = d.session_times(s[t])[1]
        pert = _run(d.perturb_after(cut), SwingBridge(use_pullback_rule, FX.UNIVERSE))
        assert _cmp(FX.decisions_until(base, s[t]), FX.decisions_until(pert, s[t])), (use_pullback_rule, t)
        reached_later.append(not _cmp(base["decisions"], pert["decisions"]) or base["rows"][-1]["equity"] != pert["rows"][-1]["equity"])
    assert any(reached_later), "the perturbation must reach the later run (non-vacuous)"


def test_A02_control_a_source_that_reads_the_next_bar_is_detected():
    d = FX.v1_double()
    s = d.sessions("0", "9")
    t = 230
    base = _run(d, LeakySource(d, FX.MSFT))
    pd = d.perturb_after(d.session_times(s[t])[1])
    pert = _run(pd, LeakySource(pd, FX.MSFT))
    assert not _cmp(FX.decisions_until(base, s[t]), FX.decisions_until(pert, s[t]))        # the leak is caught by the same comparison


# ============================================================================================================ (b) truncation
@pytest.mark.parametrize("use_pullback_rule", [True, False])
def test_B01_decisions_up_to_t_are_identical_on_data_cut_at_t(use_pullback_rule):
    d = FX.v1_double()
    s = d.sessions("0", "9")
    base = _run(d, SwingBridge(use_pullback_rule, FX.UNIVERSE))
    for t in (210, 240):
        cut = d.truncate(s[t])
        assert cut.manifest().end_session == s[t] and not cut.bars(FX.AAPL, s[t + 1], s[-1])
        trunc = _run(cut, SwingBridge(use_pullback_rule, FX.UNIVERSE))
        assert _cmp(FX.decisions_until(base, s[t]), FX.decisions_until(trunc, s[t])), (use_pullback_rule, t)
        assert any(x["action"] == "ENTRY" for x in FX.decisions_until(base, s[t]))


def test_B02_the_view_manifest_is_the_allow_list_and_a_source_using_the_dataset_end_is_detected():
    d = FX.v1_double()
    s = d.sessions("0", "9")
    view = d.as_of(d.session_times(s[100])[1])
    assert set(view.manifest()) == set(VIEW_MANIFEST_FIELDS)
    for hidden in ("end_session", "instrument_count", "content_sha256", "dataset_id", "adjustment_as_of", "retrieved_at", "source_files", "data_exposure"):
        assert hidden not in view.manifest()
    assert not hasattr(view, "execution_open")
    t = 200
    base = _run(d, EndAwareSource(d, FX.MSFT))
    cut = d.truncate(s[t])
    trunc = _run(cut, EndAwareSource(cut, FX.MSFT))
    assert not _cmp(FX.decisions_until(base, s[t]), FX.decisions_until(trunc, s[t]))      # control: the end-aware source is caught


# ============================================================================================================ (c) event timing
def _event_fixture(known):
    n = 30
    d0 = FX.simple_double({"SIM:X": FX.flat(n)}, n=n)
    s = d0.sessions("0", "9")
    e = FX.ev("E-DIV", "SIM:X", "CASH_DIVIDEND", s, 20, cash_amount=1.0, pay_session=s[22],
              **({"known_at": unknown("NOT_PROVIDED")} if known is None else {"known_at": s[known] + "T21:00:00Z"}))
    return FX.simple_double({"SIM:X": FX.flat(n)}, n=n, events=[e]), s


class DividendWatcher(SignalSource):
    """Enters X when it SEES an upcoming dividend (effective after today)."""
    role = "STRATEGY"

    def entries(self, ctx):
        upcoming = [e for e in ctx.view.events("SIM:X") if e.effective_session > ctx.session]
        return [Signal("SIM:X", raw_close=100.0, stop_raw=90.0, max_hold=2)] if upcoming else []


def test_E01_an_event_is_invisible_before_its_known_at_and_an_unknown_one_before_the_effective_open():
    d, s = _event_fixture(10)
    assert not d.as_of(d.session_times(s[9])[1]).events() and d.as_of(d.session_times(s[10])[1]).events()
    du, _ = _event_fixture(None)
    assert not du.as_of(du.session_times(s[19])[1]).events()
    assert du.as_of(du.session_times(s[20])[0]).events()                                    # visible from the open of effective_session
    pit = {"U": {"basis": "POINT_IN_TIME", "changes": (("SIM:X", s[5], "ADD", s[8] + "T21:00:00Z"),)}}
    dm = FX.simple_double({"SIM:X": FX.flat(30)}, n=30, membership=pit, basis="POINT_IN_TIME")
    assert dm.as_of(dm.session_times(s[7])[1]).membership("U", s[6]).instrument_ids == ()
    assert dm.as_of(dm.session_times(s[8])[1]).membership("U", s[6]).instrument_ids == ("SIM:X",)
    assert dm.membership("U", s[6]).instrument_ids == ("SIM:X",)                           # full history: bookings / reporting only
    sch = (ScheduledEvent("SIM:X", s[15], "AFTER_CLOSE", s[12] + "T21:00:00Z"),)
    ds = ContractDouble(sessions=s, instruments=dm.instruments(), bars=[b for b in dm.bars("SIM:X", s[0], s[-1])], manifest=dm.manifest(),
                        scheduled=sch, membership=pit)
    assert ds.as_of(ds.session_times(s[11])[1]).scheduled_events("SIM:X", s[0], s[-1]) == []
    assert len(ds.as_of(ds.session_times(s[12])[1]).scheduled_events("SIM:X", s[0], s[-1])) == 1
    assert dm.scheduled_events("SIM:X", s[0], s[-1]) is EARNINGS_DATA_UNAVAILABLE


def test_E02_decisions_see_the_event_only_from_its_known_at():
    d, s = _event_fixture(10)
    r = EN.run(d, DividendWatcher(), FX.v1_config(d, cost_model=FX.zero_costs()))
    entries = [x for x in r["decisions"] if x["action"] == "ENTRY"]
    assert entries and entries[0]["session"] == s[10]
    leak = EN.run(d.with_defect(leak_known_at=True), DividendWatcher(), FX.v1_config(d, cost_model=FX.zero_costs()))
    assert [x for x in leak["decisions"] if x["action"] == "ENTRY"][0]["session"] < s[10]           # control: the leaky view is caught


def test_E03_an_event_with_unknown_known_at_never_reaches_a_decision_before_its_effective_session():
    d, s = _event_fixture(None)
    r = EN.run(d, DividendWatcher(), FX.v1_config(d, cost_model=FX.zero_costs()))
    assert not [x for x in r["decisions"] if x["action"] == "ENTRY"]


# ============================================================================================================ (d) fill timing
def test_D01_every_fill_is_at_the_next_calendar_sessions_open_never_on_the_signal_bar():
    d = FX.v1_double()
    r = _run(d, SwingBridge(True, FX.UNIVERSE))
    assert r["fills"]
    for f in r["fills"]:
        assert f["session"] == d.next_session(f["decision_session"]) != f["decision_session"]
        assert f["price"] == d.bars(f["instrument_id"], f["session"], f["session"])[0].raw_open
    assert d.exec_calls and all(s == d.next_session(ds) for _, s, ds in d.exec_calls)


def test_D02_a_missing_next_bar_gives_no_fill_gap_and_the_order_is_not_carried():
    n = 12
    d = FX.simple_double({"SIM:X": FX.flat(n, missing=(6,)), "SIM:Y": FX.flat(n)}, n=n)
    s = d.sessions("0", "9")
    src = FX.ScriptedSource(s, entries={5: [Signal("SIM:X", notional=1000.0)], 1: [Signal("SIM:Y", notional=1000.0)]}, exits={5: ["SIM:Y"]})
    r = EN.run(d, src, FX.v1_config(d, cost_model=FX.zero_costs()))
    gaps = [x for x in r["not_taken"] if x["reason"] == "NO_FILL_GAP"]
    assert {(x["instrument_id"], x["session"]) for x in gaps} == {("SIM:X", s[6])}
    assert not [f for f in r["fills"] if f["instrument_id"] == "SIM:X"] and "NO_FILL_GAP" in r["flags"]
    d2 = FX.simple_double({"SIM:X": FX.flat(n), "SIM:Y": FX.flat(n, missing=(6,))}, n=n)
    r2 = EN.run(d2, FX.ScriptedSource(s, entries={1: [Signal("SIM:Y", notional=1000.0)]}, exits={5: ["SIM:Y"]}), FX.v1_config(d2, cost_model=FX.zero_costs()))
    assert [(x["instrument_id"], x["session"], x["reason"]) for x in r2["not_taken"]] == [("SIM:Y", s[6], "NO_FILL_GAP")]
    assert r2["open_positions"][0]["instrument_id"] == "SIM:Y"                               # the exit did not fill; nothing filled later
    assert "STALE_MARK" in r2["flags"]


def test_D03_the_provider_channel_refuses_any_session_but_the_next_one():
    d = FX.simple_double({"SIM:X": FX.flat(6)}, n=6)
    s = d.sessions("0", "9")
    assert d.execution_open("SIM:X", s[2], decision_session=s[1]) == 100.0
    for bad in (s[1], s[3]):
        with pytest.raises(ExecutionChannelViolation):
            d.execution_open("SIM:X", bad, decision_session=s[1])


def test_D04_the_engine_channel_is_armed_only_in_the_fill_phase(monkeypatch):
    d = FX.simple_double({"SIM:X": FX.flat(6)}, n=6)
    s = d.sessions("0", "9")
    orig = EN._Run._marks

    def mutant(self, day):                                               # a mutant engine that fills at the mark step
        if day == s[2]:
            self._open(EN._Order("ENTRY", "SIM:X", s[1], "SIGNAL", {}), day)
        return orig(self, day)
    monkeypatch.setattr(EN._Run, "_marks", mutant)
    with pytest.raises(ExecutionChannelViolation):
        EN.run(d, FX.ScriptedSource(s), FX.v1_config(d))

    def mutant2(self, day, i, last):                                     # a mutant engine that fills on the decision bar
        self.p.execution_open("SIM:X", day, decision_session=day)
    monkeypatch.setattr(EN._Run, "_marks", orig)
    monkeypatch.setattr(EN._Run, "_decide", mutant2)
    with pytest.raises(ExecutionChannelViolation):
        EN.run(d, FX.ScriptedSource(s), FX.v1_config(d))


def _reachable(root, limit=300_000):
    seen, out, stack = set(), [], [root]
    skip = (type, types.ModuleType, types.FunctionType, types.BuiltinFunctionType, types.CodeType, str, bytes, int, float, bool, type(None))
    while stack and len(seen) < limit:
        o = stack.pop()
        if id(o) in seen or isinstance(o, skip):
            continue
        seen.add(id(o))
        out.append(o)
        stack.extend(gc.get_referents(o))
    return out


def test_D05_the_decision_context_reaches_no_provider_and_the_control_view_is_detected():
    for defect, expect in ((False, False), (True, True)):
        d = FX.simple_double({"SIM:X": FX.flat(5)}, n=5).with_defect(view_holds_provider=defect)
        s = d.sessions("0", "9")
        src = FX.ScriptedSource(s)
        EN.run(d, src, FX.v1_config(d))
        ctx = src.contexts[0]
        found = [o for o in _reachable(ctx) if isinstance(o, ContractDouble) or (isinstance(o, types.MethodType) and isinstance(o.__self__, ContractDouble))]
        assert bool(found) is expect, defect
        assert not hasattr(ctx, "execution_open") and not hasattr(ctx.view, "execution_open") and not hasattr(ctx, "provider")


# ============================================================================================================ (i) snapshot invariance
class AbsoluteLevelSource(SignalSource):
    """NEGATIVE CONTROL: compares the ABSOLUTE split-adjusted level (the smuggled provider's unrebased factor) with the raw price."""
    role = "STRATEGY"

    def __init__(self, provider):
        self.p = provider

    def entries(self, ctx):
        b = self.p.bars(FX.AAPL, ctx.session, ctx.session)
        if b and ctx.session >= self.p.sessions("0", "9")[FX.SPLIT_INDEX] and b[0].raw_close / b[0].split_factor < 0.75 * b[0].raw_close:
            return [Signal(FX.AAPL, raw_close=b[0].raw_close, stop_raw=0.9 * b[0].raw_close, max_hold=2)]
        return []


def test_I01_the_view_rebases_factors_to_exactly_one_on_the_last_visible_bar():
    d = FX.v1_double()
    s = d.sessions("0", "9")
    for t in (100, 149, 150, 230):
        rows = d.as_of(d.session_times(s[t])[1]).bars(FX.AAPL, s[0], s[t])
        assert rows[-1].split_factor == 1.0 and rows[-1].total_return_factor == 1.0
        for c in (2.0, 0.5, 0.37, 1e6 + 1):
            sc = d.scale_factors(FX.AAPL, c).as_of(d.session_times(s[t])[1]).bars(FX.AAPL, s[0], s[t])
            for a, b in zip(rows, sc):
                assert abs(a.split_factor - b.split_factor) <= 1e-12 * abs(a.split_factor)
                if c in (2.0, 0.5):
                    assert a.split_factor == b.split_factor


@pytest.mark.parametrize("c", [2.0, 0.5, 0.37, 1e6 + 1])
def test_I02_scaling_an_instruments_factors_changes_no_decision(c):
    d = FX.v1_double()
    base = _run(d, SwingBridge(True, FX.UNIVERSE))
    scaled = _run(d.scale_factors(FX.AAPL, c), SwingBridge(True, FX.UNIVERSE))
    a, b = base["decisions"], scaled["decisions"]
    assert [(x["session"], x["action"], x["instrument_id"], x["reason"]) for x in a] == [(x["session"], x["action"], x["instrument_id"], x["reason"]) for x in b]
    assert any(x["action"] == "ENTRY" and x["instrument_id"] == FX.AAPL for x in a)
    for x, y in zip(a, b):
        for k in ("notional", "stop_raw", "target_raw"):
            if x.get(k) is not None:
                assert abs(x[k] - y[k]) <= 1e-12 * abs(x[k]), (k, x, y)
                if c in (2.0, 0.5):
                    assert x[k] == y[k]


def test_I04_the_a6_fixture_keeps_every_decision_quantity_more_than_1e_9_from_its_threshold():
    import statistics
    import swing_pullback as SP
    from swing_pullback.indicators import sma, wilder_rsi
    d = FX.v1_double()
    s = d.sessions("0", "9")
    rel = lambda a, b: abs(a - b) / max(abs(a), abs(b))  # noqa: E731
    full = {iid: d.bars(iid, s[0], s[-1]) for iid in (FX.AAPL, FX.MSFT, FX.SPY)}
    adj = {iid: [b.raw_close / b.split_factor for b in rows] for iid, rows in full.items()}
    spy200 = sma(adj[FX.SPY], SP.SLOW_SMA)
    dist = []
    for iid in (FX.AAPL, FX.MSFT):
        c, rows = adj[iid], full[iid]
        s200, s50, rsi = sma(c, SP.SLOW_SMA), sma(c, SP.FAST_SMA), wilder_rsi(c, SP.RSI_PERIOD)
        for k in range(SP.WARMUP_SESSIONS - 1, len(c)):
            dist += [rel(c[k], s200[k]), rel(adj[FX.SPY][k], spy200[k]), rel(s50[k], s50[k - SP.SLOPE_LOOKBACK]), rel(rsi[k], SP.RSI_MAX),
                     rel(c[k], SP.DEPTH_GUARD * s50[k]), rel(rows[k].raw_close, SP.MIN_PRICE)]
            dist += [rel(c[k - j], c[k - j - 1]) for j in range(SP.LOWER_CLOSES)]
            dist.append(rel(statistics.median(b.raw_close * b.raw_volume for b in rows[k - 19:k + 1]), SP.MIN_DOLLAR_VOLUME))
    r = _run(d, SwingBridge(True, FX.UNIVERSE))
    for x in (x for x in r["decisions"] if x["action"] == "ENTRY"):
        i = s.index(x["session"])
        for b in d.bars(x["instrument_id"], s[i + 1], s[min(i + 12, len(s) - 1)]):
            dist += [rel(b.raw_close, x["stop_raw"]), rel(b.raw_close, x["target_raw"])]
    assert len(dist) > 1000 and min(dist) > 1e-9, min(dist)


def test_I05_an_unavailable_factor_propagates_and_never_becomes_a_number():
    n = 4
    from md_eval.contract import unavailable
    mid = FX.simple_double({"SIM:X": FX.flat(n)}, n=n, factors={"SIM:X": [(1.0, 1.0), (unavailable("GAP"), 1.0), (1.0, 1.0), (1.0, 1.0)]})
    s = mid.sessions("0", "9")
    rows = mid.as_of(mid.session_times(s[3])[1]).bars("SIM:X", s[0], s[3])
    assert [is_missing(b.split_factor) for b in rows] == [False, True, False, False] and rows[1].split_factor.reason == "SPLIT_FACTOR_UNAVAILABLE"
    last = FX.simple_double({"SIM:X": FX.flat(n)}, n=n, factors={"SIM:X": [(2.0, 2.0), (2.0, 2.0), (2.0, 2.0), (unavailable("GAP"), 2.0)]})
    rows = last.as_of(last.session_times(s[3])[1]).bars("SIM:X", s[0], s[3])
    assert all(is_missing(b.split_factor) and b.split_factor.status == "UNAVAILABLE" and b.split_factor.reason == "LAST_VISIBLE_SPLIT_FACTOR_UNAVAILABLE"
               for b in rows) and [b.total_return_factor for b in rows] == [1.0] * 4


def test_I06_no_split_view_gives_no_signals_unless_the_run_is_flagged_split_unadjusted():
    d = FX.v1_double(views=("RAW", "TOTAL_RETURN"))
    r = _run(d, SwingBridge(True, FX.UNIVERSE))
    assert not [x for x in r["decisions"] if x["action"] == "ENTRY"] and "NO_SIGNALS_SPLIT_VIEW_UNAVAILABLE" in r["flags"]
    u = _run(d, SwingBridge(True, FX.UNIVERSE, split_unadjusted=True))
    assert [x for x in u["decisions"] if x["action"] == "ENTRY"] and "SPLIT_UNADJUSTED" in u["flags"] and "SIGNAL_BASIS_SPLIT_ADJUSTED" not in u["flags"]


def test_I07_a8_an_unknown_symbol_history_applies_its_single_symbol_everywhere_and_is_flagged():
    from md_eval.contract import Instrument, symbol_at
    one = Instrument("SIM:Z", ({"symbol": "ZZ"},), symbol_history_status="UNKNOWN")
    two = Instrument("SIM:Z", ({"symbol": "ZZ"}, {"symbol": "YY"}), symbol_history_status="UNKNOWN")
    known = Instrument("SIM:Z", ({"symbol": "ZZ", "valid_from_session": "2024-02-01", "valid_to_session": None},))
    assert symbol_at(one, "2000-01-03") == ("ZZ", True) and symbol_at(two, "2024-03-01") == (None, True)
    assert symbol_at(known, "2024-01-15") == (None, False) and symbol_at(known, "2024-03-01") == ("ZZ", False)
    base = _run(FX.v1_double(), SwingBridge(True, FX.UNIVERSE))
    unk = _run(FX.v1_double(unknown_history=(FX.MSFT, FX.SPY)), SwingBridge(True, FX.UNIVERSE))
    assert "SYMBOL_HISTORY_UNKNOWN" in unk["flags"] and "SYMBOL_HISTORY_UNKNOWN" not in base["flags"]
    assert any(x["action"] == "ENTRY" and x["instrument_id"] == FX.MSFT for x in unk["decisions"])      # matched to RESEARCH_UNIVERSE by that symbol
    assert _cmp(base["decisions"], unk["decisions"])                                                  # the same rules, the same decisions


def test_I03_control_a_source_reading_absolute_factor_levels_is_detected():
    d = FX.v1_double()
    sd = d.scale_factors(FX.AAPL, 2.0)
    a, b = _run(d, AbsoluteLevelSource(d)), _run(sd, AbsoluteLevelSource(sd))
    assert [x for x in a["decisions"] if x["action"] == "ENTRY"] != [x for x in b["decisions"] if x["action"] == "ENTRY"]
