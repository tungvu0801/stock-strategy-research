"""
I-1B-1 (S4): the MD-A -> MD-B bridge (research_gate/bridge.py) - conformance and the guard tests of every mapping row (Part A 4.1-4.4;
Part C 4.4).
  B01      MD-B's contract and look-ahead cases over {the MD-B double, the bridged MD-A dataset}: the same outcomes, except the declared,
           two-sided differences DIFF-1 (an absent instrument in the execution channel) and DIFF-3 (the view's calendar beyond the
           dataset); DIFF-4 / DIFF-5 shape the comparison (no time strings compared; structural, not value-for-value); DIFF-2 lapsed (B05)
  B02-Mn   one guard test per silent mis-mapping the bridge prevents (Missing, sentinels, integrity-only bars, unknown instruments, the
           manifest, membership by keyword, symbol history, instruments, rebased factors, scheduled events, corporate events, the calendar)
  B02-DC   two admissions of one double share no mutable state (the fresh-per-call sentinel memo) but forward the caller's recorders
  B03-B05  no instrument before its first bar; manifest_view reads no data; an unknown universe raises (O-6 parity)
"""
import gc
import types

import pytest

import historical_data_fixtures as HF
import i1_builders as I1
import md_eval_fixtures as FX
import research_gate as RG
from historical_data import calendar as CAL
from historical_data import provider as PV
from md_eval import engine as EN
from md_eval.contract import (EARNINGS_DATA_UNAVAILABLE, NO_BAR, VIEW_MANIFEST_FIELDS, CalendarNotCovered, ContractError, ExecutionChannelViolation,
                              MembershipSet, ScheduledEvent, is_missing, symbol_at)
from md_eval.signals import EqualWeight, Signal, SignalSource, SwingBridge
from md_eval.view import BookSnapshot, DecisionContext


def _code(e):
    return e.value.code


@pytest.fixture(scope="module")
def pair():
    return RG.admit_double(I1.pb_v1_double()), RG.admit(I1.pb_mda_v1_twin())


@pytest.fixture(scope="module")
def guards():
    return RG.admit(I1.pb_mda_bridge_guards())


def _iid(ds, sym):
    return next(i.instrument_id for i in ds.instruments() if i.symbol_history[0]["symbol"] == sym)


def _cfg(ds, **kw):
    s = ds.sessions("0000-00-00", "9999-99-99")
    return EN.RunConfig(start_session=s[0], end_session=s[-1], cost_model=FX.zero_costs(), initial_cash=10_000.0, **kw)


class _VisibleSums(SignalSource):
    """Records, at every decision, the sum of the visible raw closes of every instrument (a look-ahead probe; it never trades)."""
    role = "BENCHMARK"

    def __init__(self):
        self.seen = {}

    def entries(self, ctx):
        self.seen[ctx.session] = round(sum(b.raw_close for i in ctx.view.instruments() for b in ctx.view.bars(i.instrument_id, "0000-00-00", ctx.session)), 6)
        return []


# ============================================================================================================ B01 conformance
def test_B01_the_view_manifest_is_the_allow_list_on_both_providers(pair):
    for ds in pair:
        s = ds.sessions("0", "9")
        v = ds.as_of(ds.session_times(s[100])[1])
        assert set(v.manifest()) == set(VIEW_MANIFEST_FIELDS) and not hasattr(v, "execution_open") and isinstance(v.manifest()["price_views_available"], tuple)


def test_B01_the_view_rebases_factors_to_exactly_one_on_both_providers(pair):
    for ds in pair:
        s, aapl = ds.sessions("0", "9"), _iid(ds, "AAPL")
        for t in (100, 149, 150, 230):
            rows = ds.as_of(ds.session_times(s[t])[1]).bars(aapl, s[0], s[t])
            assert rows[-1].split_factor == 1.0 and rows[-1].total_return_factor == 1.0
            assert rows[0].split_factor == (2.0 if t >= 150 else 1.0)                  # rebased on the last visible bar (A6)


def test_B01_fills_are_at_the_next_sessions_open_on_both_providers(pair):
    for ds in pair:
        r = EN.run(ds, SwingBridge(True, "SYNTHETIC_UNIVERSE" if ds.manifest().provider == "SYNTHETIC" else FX.UNIVERSE), _cfg(ds))
        assert r["fills"] and any(x["action"] == "ENTRY" for x in r["decisions"])
        for f in r["fills"]:
            assert f["session"] == ds.next_session(f["decision_session"]) and f["price"] == ds.bars(f["instrument_id"], f["session"], f["session"])[0].raw_open


def test_B01_future_perturbation_changes_nothing_visible_at_or_before_t_on_both_providers():
    cases = ((RG.admit_double(I1.pb_v1_double()), RG.admit_double(I1.pb_v1_perturb_205()), 205),
             (RG.admit(I1.pb_mda_lookahead_world()), RG.admit(I1.pb_mda_lookahead_world_perturbed_s20()), 20))
    for base, pert, t in cases:
        a, b = _VisibleSums(), _VisibleSums()
        EN.run(base, a, _cfg(base))
        EN.run(pert, b, _cfg(pert))
        s = base.sessions("0", "9")
        assert all(a.seen[d] == b.seen[d] for d in s[:t + 1] if d in a.seen) and any(a.seen[d] != b.seen[d] for d in s[t + 1:-1])


def test_B01_DIFF_1_an_absent_instrument_in_the_execution_channel_is_no_bar_on_the_double_and_refused_on_the_bridge(pair):
    dbl, mda = pair
    s, t = dbl.sessions("0", "9"), mda.sessions("0", "9")
    assert dbl.execution_open("SIM:ABSENT", s[1], decision_session=s[0]) is NO_BAR
    with pytest.raises(ContractError, match="UNKNOWN_INSTRUMENT"):
        mda.execution_open("SYNTH:ABSENT:2024-01-02", t[1], decision_session=t[0])


def test_B01_DIFF_3_the_views_calendar_beyond_the_dataset_is_the_doubles_own_or_the_coverage_calendar(pair):
    dbl, mda = pair
    s, t = dbl.sessions("0", "9"), mda.sessions("0", "9")
    dv, mv = dbl.as_of(dbl.session_times(s[100])[1]), mda.as_of(mda.session_times(t[100])[1])
    assert dv.sessions(s[0], "9999-99-99") == s
    assert mv.sessions(t[0], "2026-12-31") == [x for x in CAL.SESSIONS if t[0] <= x <= "2026-12-31"] and mv.sessions(t[0], "2026-12-31")[-1] > t[-1]
    with pytest.raises(CalendarNotCovered):
        dv.next_session(s[-1])
    assert mv.next_session(t[-1]) == CAL.next_session(t[-1])


# ============================================================================================================ B02 guard tests
def test_B02_M1_a_missing_sector_and_issuer_stay_missing_to_md_eval(guards):
    aaa = next(i for i in guards.instruments() if i.instrument_id.startswith("SYNTH:AAA:"))
    assert is_missing(aaa.sector) and is_missing(aaa.issuer_id)
    run = types.SimpleNamespace(instr={aaa.instrument_id: aaa}, cfg=types.SimpleNamespace(static_map=None), static_used=False)
    assert EN._Run.sector_of(run, aaa.instrument_id) == "UNKNOWN" and SwingBridge(True, "U")._issuer(aaa) is None


def _gate_run(ds, entries, **kw):
    s = ds.sessions("0", "9")
    return EN.run(ds, FX.ScriptedSource(s, entries=entries), _cfg(ds, **kw)), s


def test_B02_M2_a_missing_next_bar_is_a_no_fill_gap(guards):
    s = guards.sessions("0", "9")
    aaa = _iid(guards, "AAA")
    assert guards.execution_open(aaa, s[4], decision_session=s[3]) is NO_BAR
    r, _ = _gate_run(guards, {3: [Signal(aaa, notional=1000.0)]})
    assert "NO_FILL_GAP" in r["flags"] and [(x["instrument_id"], x["reason"]) for x in r["not_taken"]] == [(aaa, "NO_FILL_GAP")]


def test_B02_M3_earnings_unavailable_is_md_evals_sentinel(guards):
    s = guards.sessions("0", "9")
    v = guards.as_of(guards.session_times(s[5])[1])
    assert v.scheduled_events(_iid(guards, "AAA"), s[0], s[-1]) is EARNINGS_DATA_UNAVAILABLE
    assert guards.scheduled_events(_iid(guards, "AAA"), s[0], s[-1]) is EARNINGS_DATA_UNAVAILABLE


def test_B02_M4_an_integrity_only_bar_refuses_the_run_and_is_never_a_no_fill_gap(guards):
    with pytest.raises(RG.AdmissionRefused) as e:
        _gate_run(guards, {5: [Signal(_iid(guards, "BBB"), notional=1000.0)]})
    assert _code(e) == "BRIDGE_UNMAPPABLE" and e.value.reasons == ("INTEGRITY_ONLY_BAR",)


def test_B02_M5_an_unknown_instrument_has_no_bars_and_no_gaps_and_refuses_the_execution_channel(guards):
    s = guards.sessions("0", "9")
    assert guards.bars("SYNTH:NONE:x", s[0], s[-1]) == [] and guards.gaps("SYNTH:NONE:x") == []
    with pytest.raises(ContractError, match="UNKNOWN_INSTRUMENT"):
        guards.execution_open("SYNTH:NONE:x", s[1], decision_session=s[0])


def test_B02_M6_the_manifest_carries_kind_content_and_licence_and_a_missing_retrieved_at_stays_missing(guards):
    m, raw = guards.manifest(), I1.pb_mda_bridge_guards().manifest()
    assert (m.kind, m.content_sha256, m.dataset_id) == (raw.kind, raw.content_sha256, raw.dataset_id) and m.licence == raw.licence.to_json()
    assert is_missing(m.retrieved_at) and m.retrieved_at.reason == "SYNTHETIC" and isinstance(m.start_session, str)
    assert RG.manifest_view(I1.pb_mda_bridge_guards()) == m


def test_B02_M8_membership_is_built_by_keyword_and_equal_weight_receives_the_members(guards):
    s = guards.sessions("0", "9")
    v = guards.as_of(guards.session_times(s[3])[1])
    ms = v.membership("SYNTHETIC_UNIVERSE", s[3])
    assert type(ms) is MembershipSet and ms.membership_basis == "FIXED_CURRENT_UNIVERSE" and set(ms.instrument_ids) == {_iid(guards, "AAA"), _iid(guards, "BBB")}
    ctx = DecisionContext(v, s[3], BookSnapshot(s[3], 1e4, 1e4, False, ()))
    assert sorted(x.instrument_id for x in EqualWeight("SYNTHETIC_UNIVERSE").entries(ctx)) == sorted(ms.instrument_ids)


def test_B02_M9_an_unknown_symbol_history_applies_its_single_symbol(guards):
    aaa = next(i for i in guards.instruments() if i.instrument_id.startswith("SYNTH:AAA:"))
    assert aaa.symbol_history_status == "UNKNOWN" and symbol_at(aaa, "2030-01-02") == ("AAA", True)


def test_B02_M11_a_missing_asset_type_stays_missing(guards):
    aaa = next(i for i in guards.instruments() if i.instrument_id.startswith("SYNTH:AAA:"))
    bbb = next(i for i in guards.instruments() if i.instrument_id.startswith("SYNTH:BBB:"))
    assert is_missing(aaa.asset_type) and bbb.asset_type == "COMMON_STOCK" and bbb.sector == "TECH"


def test_B02_M12_the_views_rebased_factors_equal_md_as(pair):
    _, mda = pair
    raw = I1.pb_mda_v1_twin()
    t = mda.sessions("0", "9")
    aapl = _iid(mda, "AAPL")
    for k in (140, 200):
        mine = mda.as_of(mda.session_times(t[k])[1]).bars(aapl, t[0], t[k])
        theirs = raw.as_of(CAL.close_utc(t[k])).bars(aapl, t[0], t[k])
        assert [b.split_factor for b in mine] == [b.split_factor for b in theirs] and mine[-1].split_factor == 1.0


def test_B02_M15_a_scheduled_event_is_visible_from_its_known_at():
    adm = RG.admit(I1.pb_mda_lookahead_world())
    aaa = next(i.instrument_id for i in adm.instruments() if i.instrument_id.startswith("SYNTH:AAA:"))
    before = adm.as_of(adm.session_times(HF.S[14])[1]).scheduled_events(aaa, HF.S[0], HF.S[-1])
    after = adm.as_of(adm.session_times(HF.S[15])[1]).scheduled_events(aaa, HF.S[0], HF.S[-1])
    assert before == [] and len(after) == 1 and type(after[0]) is ScheduledEvent and after[0].timing == "AFTER_CLOSE"


def test_B02_M16_a_dividend_with_an_unknown_pay_session_is_credited_on_the_ex_date_and_flagged(guards):
    aaa = _iid(guards, "AAA")
    ev = guards.events(aaa)
    assert len(ev) == 1 and is_missing(ev[0].pay_session) and ev[0].ratio is None and ev[0].child_instrument_id is None
    r, s = _gate_run(guards, {1: [Signal(aaa, notional=1000.0)]})
    credits = [e for e in r["ledger_log"] if e["kind"] == "DIVIDEND_CREDITED"]
    assert [c["session"] for c in credits] == [s[8]] and "PAY_DATE_UNKNOWN" in r["flags"]


def test_B02_M18_session_times_are_iso_z_strings_and_refuse_outside_the_dataset(guards):
    s = guards.sessions("0", "9")
    o, c = guards.session_times(s[2])
    assert o.endswith("Z") and c.endswith("Z") and (o, c) == tuple(t.isoformat().replace("+00:00", "Z") for t in CAL.session_times(s[2]))
    with pytest.raises(CalendarNotCovered):
        guards.session_times(CAL.next_session(s[-1]))


def test_B02_M19_the_datasets_sessions_are_clamped_to_its_span(guards):
    s = guards.sessions("0000-00-00", "9999-99-99")
    assert s == HF.S[:12] and guards.sessions("0", "9") == s


def test_B02_M20_the_next_session_after_the_datasets_end_is_not_covered(guards):
    s = guards.sessions("0", "9")
    with pytest.raises(CalendarNotCovered):
        guards.next_session(s[-1])
    assert guards.next_session(s[0]) == s[1]


def test_B02_M21_the_decision_time_round_trips_and_a_naive_time_is_refused(guards):
    s = guards.sessions("0", "9")
    t = guards.session_times(s[4])[1]
    assert guards.as_of(t).decision_time_utc == t
    with pytest.raises(ValueError):
        guards.as_of(t[:-1])


def test_B02_M23_the_view_manifest_has_the_a11_fields_only(guards):
    s = guards.sessions("0", "9")
    m = guards.as_of(guards.session_times(s[4])[1]).manifest()
    assert set(m) == set(VIEW_MANIFEST_FIELDS) and "end_session" not in m and isinstance(m["price_views_available"], tuple)


def test_B02_M24_the_execution_channel_timing_error_is_md_evals(guards):
    s = guards.sessions("0", "9")
    with pytest.raises(ExecutionChannelViolation):
        guards.execution_open(_iid(guards, "AAA"), s[3], decision_session=s[1])
    with pytest.raises(PV.ExecutionTimingError):
        PV._TableProvider.execution_open(I1.pb_mda_bridge_guards(), _iid(guards, "AAA"), s[3], decision_session=s[1])


def _mutables(root, skip_ids):
    seen, out, stack = set(), set(), [root]
    skip = (type, types.ModuleType, types.FunctionType, types.BuiltinFunctionType, types.CodeType, str, bytes, int, float, bool, type(None))
    while stack:
        o = stack.pop()
        if id(o) in seen or id(o) in skip_ids or isinstance(o, skip):
            continue
        seen.add(id(o))
        if type(o) in (list, dict):
            out.add(id(o))
        stack.extend(gc.get_referents(o))
    return out


def test_B02_DC_two_admissions_of_one_double_share_no_mutable_state_but_forward_the_recorders():
    d = I1.pb_flat5()
    a, b = RG.admit_double(d), RG.admit_double(d)
    sa, sb = a._snapshot, b._snapshot                                                   # test-only reach-in
    rec = {id(d.calls), id(d.exec_calls)}
    shared = (_mutables(sa, rec) & _mutables(sb, rec)) - {id(vars(sa)), id(vars(sb))}
    assert shared == set()
    s = a.sessions("0", "9")
    assert a.scheduled_events("SIM:X", s[0], s[-1]) is EARNINGS_DATA_UNAVAILABLE is b.scheduled_events("SIM:X", s[0], s[-1])
    assert a.admission.admission_sha256 == b.admission.admission_sha256
    assert sa.calls is d.calls is sb.calls and sa.exec_calls is d.exec_calls is sb.exec_calls


# ============================================================================================================ B03-B05
def test_B03_an_instrument_first_listed_after_t_is_not_in_the_view_at_t():
    adm = RG.admit(I1.pb_mda_lookahead_world())
    ccc = next(i.instrument_id for i in adm.instruments() if i.instrument_id.startswith("SYNTH:CCC:"))
    assert ccc not in {i.instrument_id for i in adm.as_of(adm.session_times(HF.S[20])[1]).instruments()}
    assert ccc in {i.instrument_id for i in adm.as_of(adm.session_times(HF.S[25])[1]).instruments()}


class _Spy:
    def __init__(self, m):
        self.m, self.calls = m, []

    def manifest(self):
        self.calls.append("manifest")
        return self.m

    def __getattr__(self, name):
        self.calls.append(name)
        raise AttributeError(name)


def test_B04_manifest_view_on_a_spy_reads_no_data_and_carries_the_manifest():
    raw = I1.pb_mda_bridge_guards()
    spy = _Spy(raw.manifest())
    m = RG.manifest_view(spy)
    assert spy.calls == ["manifest"] and (m.kind, m.content_sha256) == (raw.manifest().kind, raw.manifest().content_sha256)
    assert m.licence == raw.manifest().licence.to_json()


def test_B04b_manifest_view_on_a_contract_double_records_exactly_one_manifest_call():
    d = I1.pb_flat5()
    RG.manifest_view(d)
    assert d.calls == ["manifest"]


def test_B05_an_unknown_universe_raises_on_the_dataset_and_on_the_view():
    adm = RG.admit(I1.pb_mda_lookahead_world())
    with pytest.raises(ContractError):
        adm.membership("WRONG", HF.S[5])
    with pytest.raises(ContractError):
        adm.as_of(adm.session_times(HF.S[5])[1]).membership("WRONG", HF.S[5])
    assert adm.as_of(adm.session_times(HF.S[5])[1]).membership(HF.U, HF.S[5]).instrument_ids
