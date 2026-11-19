"""
I-1B-1 (S4): the admission snapshot and its time-of-check / time-of-use guarantees (Part C 4.2).
  T01-T06    the caller's objects mutated after admission change nothing; the gate's snapshot mutated before or during a run refuses; a source
             file edited during or before a run refuses; a swapped admission handle; a provider changed before admission; side-channel tampering
  RT01-RT04  the strict loader round-trips every registered adapter's fixture (canonical bytes, record sha, side channel, query-state hash)
  T11-T14    an edge-removed row refuses; source files resolve under their root; the double snapshot keeps the sentinels' identity; verify's
             structural checks (documenting RR-1: in-process reach-in is detected, not prevented)
Synthetic data only: MS-1-shaped and sip_daily_v1-shaped archives are written by test helpers under tmp_path (or a fresh temporary root).
"""
import json
import sys
from pathlib import Path

import pytest

import historical_data_fixtures as HF
import i1_builders as I1
import md_eval_fixtures as FX
import research_gate as RG
from historical_data import canonical as CN
from historical_data import validate as VA
from historical_data.adapters import ms1_evidence as MS1
from historical_data.adapters import sip_daily_v1 as SIP
from historical_data.provider import _TableProvider
from md_eval import engine as EN
from md_eval.contract import EARNINGS_DATA_UNAVAILABLE, NO_BAR
from md_eval.signals import BuyAndHold, Signal
RGA = sys.modules["research_gate.admit"]                                        # the module: the package attribute `admit` is the admit() function
from research_gate import snapshot as SN


def _code(e):
    return e.value.code


def _cfg(ds):
    s = ds.sessions("0000-00-00", "9999-99-99")
    return EN.RunConfig(start_session=s[0], end_session=s[-1], cost_model=FX.zero_costs(), initial_cash=10_000.0)


class _SpyLedger:
    def __init__(self):
        self.recorded = []

    def head(self):
        return None

    def record_validation_run(self, *a, **k):
        self.recorded.append(a)

    record_test_run = record_validation_run


class _Acting(FX.ScriptedSource):
    """A scripted source that runs `act` once, at the decision of session index `at` (the run's own reach-in, test-only)."""

    def __init__(self, sessions, at, act):
        super().__init__(sessions)
        self.at, self.act, self.done = at, act, False

    def entries(self, ctx):
        if not self.done and self.idx[ctx.session] == self.at:
            self.done = True
            self.act()
        return super().entries(ctx)


def _first_iid(ds):
    return sorted(i.instrument_id for i in ds.instruments())[0]


# ============================================================================================================ T01-T06
def test_T01_mutating_the_callers_objects_after_admission_changes_nothing():
    p, ref = I1.pb_mda_lookahead_world(), RG.admit(I1.pb_mda_lookahead_world())
    adm = RG.admit(p)
    iid = _first_iid(adm)
    p._bars, p._by_inst = (), {}
    a, b = EN.run(adm, BuyAndHold(iid), _cfg(adm)), EN.run(ref, BuyAndHold(iid), _cfg(ref))
    assert a["fills"] and (a["rows"], a["fills"], a["decisions"]) == (b["rows"], b["fills"], b["decisions"])
    d = I1.pb_flat5()
    dadm, dref = RG.admit_double(d), RG.admit_double(I1.pb_flat5())
    d._bars.clear()
    x, y = EN.run(dadm, BuyAndHold("SIM:X"), _cfg(dadm)), EN.run(dref, BuyAndHold("SIM:X"), _cfg(dref))
    assert x["fills"] and (x["rows"], x["fills"]) == (y["rows"], y["fills"])


def test_T02a_the_snapshot_changed_between_admission_and_the_run_is_refused():
    adm = RG.admit_double(I1.pb_flat5())
    adm._snapshot._bars["SIM:X"] = adm._snapshot._bars["SIM:X"][:2]           # test-only reach-in (RR-1: detected, not prevented)
    with pytest.raises(RG.AdmissionRefused) as e:
        EN.run(adm, FX.ScriptedSource(adm.sessions("0", "9")), _cfg(adm))
    assert _code(e) == "SNAPSHOT_CHANGED"


def test_T02b_the_snapshot_changed_during_the_run_is_refused_at_the_end_and_nothing_is_recorded():
    adm = RG.admit_double(I1.pb_flat10())
    snap = adm._snapshot
    s = adm.sessions("0", "9")
    src = _Acting(s, 3, lambda: snap._bars.__setitem__("SIM:X", snap._bars["SIM:X"][:2]))
    spy = _SpyLedger()
    with pytest.raises(RG.AdmissionRefused) as e:
        EN.run(adm, src, _cfg(adm), holdout=spy)
    assert _code(e) == "SNAPSHOT_CHANGED" and src.done and spy.recorded == []


def _sip():
    b = I1.pb_sip_invented_bundle()
    return b.root, RG.admit_from_source(b.adapter_id, b.root)


def test_T03_a_source_file_edited_during_the_run_is_refused_at_the_end_and_nothing_is_recorded():
    root, adm = _sip()
    s = adm.sessions("0", "9")
    target = root / "sip_bundle.json"
    src = _Acting(s, 5, lambda: target.write_bytes(target.read_bytes() + b" "))
    spy = _SpyLedger()
    with pytest.raises(RG.AdmissionRefused) as e:
        EN.run(adm, src, _cfg(adm), holdout=spy)
    assert _code(e) == "SOURCE_CHANGED_DURING_RUN" and src.done and spy.recorded == []


def test_T03b_a_source_file_edited_before_the_run_is_refused_before_any_evaluation_read():
    root, adm = _sip()
    raw = root / "raw" / "bars_raw_01_p001.json.gz"
    raw.write_bytes(raw.read_bytes()[:-1])
    src = FX.ScriptedSource(adm.sessions("0", "9"))
    with pytest.raises(RG.AdmissionRefused) as e:
        EN.run(adm, src, _cfg(adm))
    assert _code(e) == "SOURCE_CHANGED_DURING_RUN" and src.contexts == []


def test_T04_a_swapped_admission_handle_is_a_mismatch():
    a, b = RG.admit_double(I1.pb_flat5()), RG.admit_double(I1.pb_flat6())
    with pytest.raises(RG.NotAdmitted) as e:
        EN.run(a, FX.ScriptedSource(a.sessions("0", "9")), _cfg(a), admission=b.admission)
    assert _code(e) == "ADMISSION_MISMATCH"


def test_T04b_a_presented_admission_with_a_raw_provider_is_checked_too():
    d = FX.v1_double()
    other = RG.admit_double(I1.pb_flat5()).admission
    with pytest.raises(RG.NotAdmitted) as e:
        EN.run(d, FX.ScriptedSource(d.sessions("0", "9")), FX.v1_config(d), admission=other)
    assert _code(e) == "ADMISSION_MISMATCH"
    with pytest.raises(RG.NotAdmitted) as e:
        EN.run(d, FX.ScriptedSource(d.sessions("0", "9")), FX.v1_config(d), admission=object.__new__(RG.Admission))
    assert _code(e) == "NOT_ADMITTED"


def test_T05_a_provider_changed_before_admission_is_stale():
    p = I1.pb_mda_lookahead_world()
    p._bars = p._bars[:-1]                                                     # test-only: the content no longer matches its manifest
    with pytest.raises(RG.AdmissionRefused) as e:
        RG.admit(p)
    assert _code(e) == "CONTENT_HASH_STALE"


def test_T06_side_channel_tampering_is_refused_by_the_entry_guard_or_the_removed_row_cross_check():
    for tamper in (lambda p: setattr(p, "_dropped", ("ZZQX",)), lambda p: setattr(p, "_notes", p._notes + ({"tamper": 1},))):
        p = I1.pb_mda_lookahead_world()
        tamper(p)
        with pytest.raises(RG.AdmissionRefused) as e:
            RG.admit(p)
        assert _code(e) == "ALLOWLIST_GUARD_FAILED" and e.value.reasons == ("SIDE_CHANNEL_MISMATCH",)
    p = I1.pb_mda_lookahead_world()
    p._notes = ({"removed_invalid": []},)                                      # a removal note dropped
    with pytest.raises(RG.AdmissionRefused) as e:
        RG.admit(p)
    assert _code(e) == "SNAPSHOT_ROUNDTRIP_FAILED"


# ============================================================================================================ RT01-RT04
def _roundtrip(p):
    data = CN.dumps(_TableProvider.canonical_document(p))
    side = SN.side_channel_of(p)
    a, b = SN.load_snapshot(data, side), SN.load_snapshot(data, side)
    assert CN.dumps(_TableProvider.canonical_document(a)) == data                                   # (i)
    assert VA.validate(a)["record_sha256"] == VA.validate(p)["record_sha256"]                      # (ii)
    assert SN.side_channel_of(a) == side and SN.roundtrip_problems(a, data, CN.sha256_bytes(data), side) == []   # (iii)
    assert SN.query_state_sha256(a) == SN.query_state_sha256(b) and SN.structure_ok(a)            # (iv)


def test_RT01_the_lookahead_world_round_trips():
    _roundtrip(I1.pb_mda_lookahead_world())


def test_RT02_a_section_5_provider_round_trips():
    _roundtrip(HF.provider(HF.series("AAA", HF.S[:15], [100.0 + k for k in range(15)]), HF.series("BBB", HF.S[3:15], [50.0 + k for k in range(12)])))


def test_RT03_a_synthetic_ms1_shaped_archive_round_trips(tmp_path):
    bars = [HF.ms1_bar(s, 10.0 + k, 11.0 + k, 9.0 + k, 10.5 + k) for k, s in enumerate(HF.S[:6])]
    raw = HF.write_ms1_archive(tmp_path, {"ZZAA": bars, "ZZBB": bars[1:]})
    _roundtrip(MS1.Ms1EvidenceProvider(raw))


def test_RT04_a_sip_bundle_round_trips(tmp_path):
    (tmp_path / "raw").mkdir()
    for rel, data in I1.sip_bundle_files().items():
        (tmp_path / rel).write_bytes(data)
    _roundtrip(SIP.SipDailyV1Provider(tmp_path))


# ============================================================================================================ T11-T14
def test_T11_a_removed_row_after_the_last_valid_bar_refuses_the_round_trip():
    rows = [dict(instrument_id="SYNTH:AAA:" + HF.S[0], session=s, o=100.0, h=101.0, l=99.0, c=100.0, v=1_000_000.0) for s in HF.S[:5]]
    rows.append(dict(instrument_id="SYNTH:AAA:" + HF.S[0], session=HF.S[5], o=100.0, h=101.0, l=150.0, c=100.0, v=1_000_000.0))   # invalid, at the edge
    p = I1.SY.SyntheticProvider(instruments=[I1.SY.instrument("AAA", HF.S[0])], rows=rows)
    with pytest.raises(RG.AdmissionRefused) as e:
        RG.admit(p)
    assert _code(e) == "SNAPSHOT_ROUNDTRIP_FAILED" and "REMOVED_ROWS" in e.value.reasons


def test_T12_every_source_file_resolves_under_its_root():
    root, adm = _sip()
    files = adm.admission.source_files
    assert {f[0] for f in files} == {"sip_bundle.json", "manifest.json", "raw/bars_raw_01_p001.json.gz"}
    for path, _, _ in files:
        p = MS1.inside(Path(root), path)
        assert p.is_file() and p.is_relative_to(Path(root).resolve())
    assert adm.admission.source_root_id and str(root) not in json.dumps(list(files))


def test_T13_the_double_snapshot_keeps_the_sentinels_identity():
    adm = RG.admit_double(I1.pb_la_d02_x_missing6())
    s = adm.sessions("0", "9")
    assert adm.scheduled_events("SIM:X", s[0], s[-1]) is EARNINGS_DATA_UNAVAILABLE
    assert adm.execution_open("SIM:X", s[6], decision_session=s[5]) is NO_BAR
    assert adm.as_of(adm.session_times(s[4])[1]).scheduled_events("SIM:X", s[0], s[-1]) is EARNINGS_DATA_UNAVAILABLE


def test_T14_verify_refuses_an_added_attribute_a_rebound_index_and_an_instance_method_override():
    a = RG.admit_double(I1.pb_flat5())
    object.__setattr__(a._snapshot, "x", 1)                                    # test-only reach-in (RR-1)
    with pytest.raises(RG.AdmissionRefused) as e:
        EN.run(a, FX.ScriptedSource(a.sessions("0", "9")), _cfg(a))
    assert _code(e) == "SNAPSHOT_CHANGED"
    b = RG.admit(I1.pb_mda_lookahead_world())
    object.__setattr__(b._snapshot, "_by_inst", dict(b._snapshot._by_inst))   # equal content, so the hash is unchanged: the type check refuses
    assert SN.query_state_sha256(b._snapshot) == b._qsha
    with pytest.raises(RG.AdmissionRefused) as e:
        RG.verify(b)
    assert _code(e) == "SNAPSHOT_CHANGED"
    c = RG.admit_double(I1.pb_flat6())
    object.__setattr__(c._snapshot, "bars", lambda *x: [])
    with pytest.raises(RG.AdmissionRefused) as e:
        RG.verify(c)
    assert _code(e) == "SNAPSHOT_CHANGED"
    assert RGA.verify is RG.verify
