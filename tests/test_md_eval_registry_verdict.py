"""
MD-B (md_eval): MD-4 (the pre-registration, the trial registry, the holdout ledger), the verdict, the licence rule and isolation.
  P01-P04  the template refuses unknown / missing keys, a changed book, an unset threshold, overlapping or missing segments, bad costs / cash /
           delisting / initial cash / static map / dsr basis; a stable sha256
  G01-G04  (G02 unused) hash-chained append-only files: an edit, a removal or a reorder refuses the file; priors reconstructed once, lower bounds;
           k counts overlapping and UNKNOWN-range priors (all of them when the exposure is UNKNOWN)
  H01-H04  a TEST look needs the pre-registration recorded BEFORE it; the holdout is keyed on the CALENDAR range: a re-look at an overlapping range
           is refused even on another dataset (MED-1 c); the exposure flags; a pre-registration recorded after the look cannot open that TEST
  R01-R05  engine.run's segment guard (MED-1 b): mislabelled / straddling ranges, a TEST overlap without TEST_OPENED, a missing holdout refused;
           runs recorded with identity, result and series sha256; chain heads (LOW-1); one TEST attempt per source and cost level, one
           TEST_RESULT per TEST_OPENED (MED-3)
  V01-V14  the verdict (MED-1 a, d; MED-3; LOW-1, LOW-6): no threshold, statistic, series or trial count parameter; ESTABLISHED only on an opened
           TEST segment, once; the caps; tampering, an unrecorded run, a rebuilt ledger, a substituted series refused; the exact segment, the
           pre-registered settings and the strategy / control identity are required; DSR on the declared basis with the registry's k
  LC01-LC06 the licence rule: HISTORICAL UNKNOWN / NOT_PERMITTED / CONFIRMED-without-the-file refused before any data call; SYNTHETIC exempt and
           labelled; a CONFIRMED HISTORICAL run needs its registered segment; the benchmarks, the signal bridge and the random control share the
           gate (MED-2, LOW-8); the random control runs as a registered, recorded VALIDATION run, role RANDOM_CONTROL (LOW-N1)
  X01-X03  isolation: no socket or SQLite during a run; md_eval imports only the standard library and the three reused packages; it never
           names the 7.8B-1 evaluator or the research-data package
"""
import ast
import copy
import inspect
import socket
import sqlite3
from pathlib import Path

import pytest

import md_eval_fixtures as FX
from md_eval import benchmarks as BM
from md_eval import cash_rate as CR
from md_eval import engine as EN
from md_eval import inference as IN
from md_eval import prereg as PR
from md_eval import prior_trials as PT
from md_eval import registry as RG
from md_eval import verdict as VD
from md_eval.contract import LicenceRefused
from md_eval.costs import ZERO_COST_BUCKETS, CostModel
from md_eval.isolation import IsolationViolation
from md_eval.signals import BuyAndHold, Signal, SignalSource, SwingBridge
from md_eval.view import BookSnapshot, DecisionContext
import research_gate as RGATE

ROOT = Path(__file__).resolve().parents[1]
DATA = "d" * 64
V1_HASH = "6c20fdb4f9c093fed0a82c21c642dfceee2cc6d424104390c7c432e3cfa61903"
CONFIRMED = {"licence_ref": "owner-file", "personal_research_use": "CONFIRMED", "redistribution": "NONE", "licence_file_sha256": "f" * 64,
             "confirmed_by": "owner", "confirmed_on": "2026-11-16"}


def _prereg(**over):
    p = PR.template()
    p.update({"hypothesis_id": "H-V1", "family": ["H-V1", "H-C1"], "dataset_sha256": DATA, "author": "test", "created_at": "2026-11-16T00:00:00Z",
              "segments": {"TRAIN": ["2016-01-04", "2019-12-31"], "VALIDATION": ["2020-01-02", "2022-12-30"], "TEST": ["2023-01-03", "2024-12-31"]},
              "thresholds": {"alpha": 0.05, "n_min_sessions": 250, "min_dsr": None}})
    p["strategy"]["preregistration_hash"] = V1_HASH
    p["benchmark_instrument"] = "SIM:SPY"
    p.update(over)
    return p


# ============================================================================================================ the pre-registration (P)
def test_P01_the_template_is_refused_until_every_threshold_is_fixed_and_unknown_keys_are_refused():
    with pytest.raises(PR.PreregError):
        PR.validate(PR.template())                                                     # alpha None: never chosen later
    p = _prereg()
    sha = PR.validate(p)
    assert sha == PR.validate(copy.deepcopy(p)) and len(sha) == 64
    for bad in (dict(p, extra=1), {k: v for k, v in p.items() if k != "thresholds"}, dict(p, book=dict(p["book"], max_positions=5)),
                dict(p, family=["H-C1"]), dict(p, costs=dict(p["costs"], multipliers=[1.0, 3.0]))):
        with pytest.raises(PR.PreregError):
            PR.validate(bad)


def test_P02_a_changed_threshold_is_a_different_pre_registration():
    a, b = _prereg(), _prereg(thresholds={"alpha": 0.10, "n_min_sessions": 250, "min_dsr": None})
    assert PR.validate(a) != PR.validate(b)


def test_P03_segments_are_chronological_never_overlap_and_name_a_test():
    for segs in ({"TRAIN": ["2016-01-04", "2020-06-30"], "VALIDATION": ["2020-01-02", "2022-12-30"], "TEST": ["2023-01-03", "2024-12-31"]},
                 {"TRAIN": ["2016-01-04", "2019-12-31"], "VALIDATION": ["2020-01-02", "2022-12-30"]},
                 {"VALIDATION": ["2020-01-02", "2023-06-30"], "TEST": ["2023-01-03", "2024-12-31"]}):
        with pytest.raises(PR.PreregError):
            PR.validate(_prereg(segments=segs))


def test_P04_costs_cash_delisting_initial_cash_static_map_and_dsr_basis_are_validated():
    p = _prereg()
    for bad in (dict(p, costs={k: v for k, v in p["costs"].items() if k != "fees"}), dict(p, costs=dict(p["costs"], fees="yes")),
                dict(p, cash_rate={"mode": "T_BILLS"}), dict(p, delisting_primary="LAST_PRICE"), dict(p, initial_cash=0.0),
                dict(p, initial_cash=100000), dict(p, static_map_sha256="abc"), dict(p, dsr_basis="RAW")):
        with pytest.raises(PR.PreregError):
            PR.validate(bad)
    for basis in PR.DSR_BASES:
        assert PR.validate(dict(p, dsr_basis=basis))
    assert PR.template()["dsr_basis"] == "STRATEGY_EXCESS_OVER_CASH" and PR.template()["costs"]["fees"] is True


# ============================================================================================================ the chains (G)
def test_G01_append_only_hash_chain_refuses_edits_removals_and_reorders(tmp_path):
    reg = RG.TrialRegistry(tmp_path / "trials.jsonl", clock=lambda: "2026-11-16T00:00:00Z")
    reg.register(_prereg())
    reg.register(_prereg(hypothesis_id="H-C1"))
    lines = (tmp_path / "trials.jsonl").read_text(encoding="utf-8").split("\n")
    assert len(reg.entries()) == 2 and lines[2] == ""
    for broken in (lines[0].replace("H-V1", "H-XX") + "\n" + lines[1] + "\n", lines[1] + "\n", lines[1] + "\n" + lines[0] + "\n"):
        (tmp_path / "trials.jsonl").write_text(broken, encoding="utf-8")
        with pytest.raises(RG.ChainError):
            reg.entries()
        with pytest.raises(RG.ChainError):
            reg.register(_prereg(hypothesis_id="H-NEW", family=["H-NEW"]))


def test_G03_priors_are_reconstructed_once_as_lower_bounds(tmp_path):
    reg = RG.TrialRegistry(tmp_path / "t.jsonl")
    got = reg.reconstruct_priors()
    assert len(got) == len(PT.PRIOR_TRIALS) and all(e["payload"]["count_is_lower_bound"] is True for e in got)
    assert not any("SWING_TREND_PULLBACK" in str(e["payload"]) for e in got)                  # V1 / C1 never ran on real data
    with pytest.raises(RG.ChainError):
        reg.reconstruct_priors()


def test_G04_the_trial_count_is_conservative(tmp_path):
    reg = RG.TrialRegistry(tmp_path / "t.jsonl")
    reg.reconstruct_priors()
    reg.register(_prereg())
    with pytest.raises(RG.ChainError):
        reg.register(_prereg())                                                             # registers once
    every = sum(p["variants_lower_bound"] for p in PT.PRIOR_TRIALS)
    assert reg.trial_count(DATA, "2016-01-04", "2019-12-31", data_exposure_unknown=True)["k"] == every + 1 == 31
    unk = sum(p["variants_lower_bound"] for p in PT.PRIOR_TRIALS if not isinstance(p["range"], list))
    assert reg.trial_count(DATA, "2016-01-04", "2019-12-31", data_exposure_unknown=False)["k"] == unk + 1 == 27
    assert reg.trial_count(DATA, "2021-01-04", "2021-12-31", data_exposure_unknown=False)["k"] == every + 1
    assert reg.trial_count("e" * 64, "2016-01-04", "2019-12-31", data_exposure_unknown=False)["k"] == unk


# ============================================================================================================ the holdout ledger (H)
def test_H01_a_test_look_needs_the_pre_registration_recorded_before_it(tmp_path):
    led = RG.HoldoutLedger(tmp_path / "h.jsonl")
    p = _prereg()
    with pytest.raises(RG.HoldoutRefused):
        led.open_test(p, DATA)
    rec = led.record_prereg(p)
    op = led.open_test(p, DATA)
    assert op["payload"]["prereg_seq"] == rec["seq"] < op["seq"] and op["payload"]["family"] == ["H-V1", "H-C1"]
    assert led.test_open_entry(PR.validate(p), DATA) == op
    assert led.record_validation_run(PR.validate(p), DATA, "i" * 64, "r" * 64)["payload"]["attempt"] == 1
    assert led.record_validation_run(PR.validate(p), DATA, "j" * 64, "s" * 64)["payload"]["attempt"] == 2
    assert led.run_entry("VALIDATION_RUN", PR.validate(p), DATA, "j" * 64)["payload"]["result_sha256"] == "s" * 64


def test_H02_one_look_spends_the_calendar_range_whatever_the_dataset(tmp_path):
    led = RG.HoldoutLedger(tmp_path / "h.jsonl")
    a = _prereg()
    led.record_prereg(a)
    led.open_test(a, DATA)
    b = _prereg(hypothesis_id="H-OTHER", family=["H-OTHER"])
    led.record_prereg(b)
    with pytest.raises(RG.HoldoutRefused):
        led.open_test(b, DATA)                                                              # a different hypothesis: still spent
    with pytest.raises(RG.HoldoutRefused):
        led.open_test(b, "e" * 64)                                                          # MED-1 c: a re-look through another dataset sha
    later = _prereg(hypothesis_id="H-LATER", family=["H-LATER"], segments=dict(a["segments"], TEST=["2025-01-02", "2025-12-31"]))
    led.record_prereg(later)
    assert led.open_test(later, "e" * 64)["payload"]["test_range"] == ["2025-01-02", "2025-12-31"]   # a later, unspent calendar range


def test_H03_the_test_range_exposure_is_flagged(tmp_path):
    led = RG.HoldoutLedger(tmp_path / "h.jsonl")
    p1 = _prereg()
    led.record_prereg(p1)
    assert led.open_test(p1, DATA)["payload"]["flags"] == ["DATA_EXPOSURE_UNKNOWN"]
    p2 = _prereg(hypothesis_id="H-2", family=["H-2"], segments=dict(p1["segments"], TEST=["2025-01-02", "2025-12-31"]))
    led.record_prereg(p2)
    seen = [{"study": "Stage 6.4", "range": ["2025-06-02", "2025-06-30"]}]
    assert led.open_test(p2, DATA, data_exposure=seen)["payload"]["flags"] == ["TEST_RANGE_PREVIOUSLY_EXPOSED"]
    p3 = _prereg(hypothesis_id="H-3", family=["H-3"], segments=dict(p1["segments"], TEST=["2026-01-02", "2026-06-30"]))
    led.record_prereg(p3)
    assert led.open_test(p3, DATA, data_exposure=seen)["payload"]["flags"] == []


def test_H04_a_pre_registration_recorded_after_the_look_cannot_open_that_test(tmp_path):
    led = RG.HoldoutLedger(tmp_path / "h.jsonl")
    a = _prereg()
    led.record_prereg(a)
    led.open_test(a, DATA)
    tuned = _prereg(thresholds={"alpha": 0.2, "n_min_sessions": 10, "min_dsr": None})           # "thresholds chosen after the look"
    led.record_prereg(tuned)
    with pytest.raises(RG.HoldoutRefused):
        led.open_test(tuned, DATA)


# ============================================================================================================ a small synthetic world
N_WORLD = 60
ID_EXTRA = {"version": "1.0", "preregistration_hash": V1_HASH}          # what SwingBridge's identity carries for V1 and C1


def _world(tmp_path, name="w", *, rising=True, thresholds=None, views=("RAW", "SPLIT_ADJUSTED", "TOTAL_RETURN"), benchmark="SIM:SPY", spy_rising=False,
           dsr_basis="STRATEGY_EXCESS_OVER_CASH", priors=True, cash_rate=None, dataset=None):
    """X rises 1 % a session (flat when not rising), SPY flat (or rising 0.5 %); POINT_IN_TIME basis; TRAIN 0-9, VALIDATION 10-29, TEST 30-59.
    The pre-registration fixes the template's ASSUMPTION costs (fees on), embargo 0, ZERO cash and 10 000 USD."""
    closes = [100.0 * (1.01 ** k if rising else 1.0) for k in range(N_WORLD)]
    spy = [100.0 * (1.005 ** k if spy_rising else 1.0) for k in range(N_WORLD)]
    prices = {"SIM:X": [((closes[k - 1] if k else closes[k]), closes[k]) for k in range(N_WORLD)],
              "SIM:SPY": [((spy[k - 1] if k else spy[k]), spy[k]) for k in range(N_WORLD)]}
    d = FX.simple_double(prices, n=N_WORLD, basis="POINT_IN_TIME", views=views)
    s = d.sessions("0", "9")
    p = _prereg(dataset_sha256=dataset or d.manifest().content_sha256, segments={"TRAIN": [s[0], s[9]], "VALIDATION": [s[10], s[29]], "TEST": [s[30], s[59]]},
                bootstrap={"mean_block_sessions": 5, "resamples": 200, "seed": 7, "confidence": 0.9},
                thresholds=thresholds or {"alpha": 0.10, "n_min_sessions": 10, "min_dsr": None}, benchmark_instrument=benchmark,
                embargo_sessions=0, initial_cash=10_000.0, dsr_basis=dsr_basis, cash_rate=cash_rate or {"mode": "ZERO"})
    clock = lambda: f"2026-11-16T00:00:00Z#{name}"  # noqa: E731 - each world's chain is distinct, so a run never binds to another world's ledger
    reg = RG.TrialRegistry(tmp_path / name / "trials.jsonl", clock=clock)
    led = RG.HoldoutLedger(tmp_path / name / "holdout.jsonl", clock=clock)
    if priors:
        reg.reconstruct_priors()                                                        # LOW-R1: the verdict requires them (k = 1 + 30)
    reg.register(p)
    led.record_prereg(p)
    return {"d": d, "s": s, "p": p, "sha": PR.validate(p), "reg": reg, "led": led, "closes": closes}


def _cfg(w, label, lo, hi, mult=1.0, **over):
    """A run configured exactly as the pre-registration says (its costs, embargo, cash, initial cash), unless `over` changes something."""
    c, cr = w["p"]["costs"], w["p"]["cash_rate"]
    kw = dict(start_session=w["s"][lo], end_session=w["s"][hi], initial_cash=w["p"]["initial_cash"], prereg_sha256=w["sha"], segment_label=label,
              embargo_sessions=w["p"]["embargo_sessions"], cost_model=CostModel(buckets=c["buckets"], multiplier=mult, fees=c["fees"], label=c["label"]),
              cash_rate=CR.hypothetical(cr["annual"]) if cr["mode"] == "HYPOTHETICAL_CONSTANT" else CR.ZERO)
    kw.update(over)
    return EN.RunConfig(**kw)


def _runs(w, label, lo, hi, *, src_flags=(), control_name=None, control_entries=False, over=None, strat_extra=ID_EXTRA, control_extra=ID_EXTRA):
    s, c = w["s"], w["closes"][lo]
    over = over or {}
    entries = {lo: [Signal("SIM:X", raw_close=c, stop_raw=0.9 * c)]}
    strat = lambda: FX.ScriptedSource(s, entries=entries, role="STRATEGY", name=w["p"]["strategy"]["id"], flags=src_flags, identity_extra=strat_extra)  # noqa: E731
    ctrl = FX.ScriptedSource(s, entries=entries if control_entries else {}, role="STRATEGY", name=control_name or w["p"]["control"]["id"],
                             identity_extra=control_extra)
    kw = {"registry": w["reg"], "holdout": w["led"]}
    res = {"strategy": EN.run(w["d"], strat(), _cfg(w, label, lo, hi, **over.get("strategy", {})), **kw),
           "strategy_2x": EN.run(w["d"], strat(), _cfg(w, label, lo, hi, **{"mult": 2.0, **over.get("strategy_2x", {})}), **kw),
           "control": EN.run(w["d"], ctrl, _cfg(w, label, lo, hi, **over.get("control", {})), **kw)}
    return res, res["strategy"]["benchmark_returns"]                                      # the bound series, computed by the engine


def _verdict(w, res):
    return VD.verdict(res, w["p"], w["led"], w["reg"])


# ============================================================================================================ the segment guard (R)
def test_R01_a_mislabelled_or_straddling_range_and_an_unopened_test_are_refused(tmp_path):
    w = _world(tmp_path)
    src = lambda: FX.ScriptedSource(w["s"], role="STRATEGY")  # noqa: E731
    for label, lo, hi in (("VALIDATION", 30, 40), ("TEST", 12, 20), ("VALIDATION", 25, 35), ("TRAIN", 5, 12), (None, 12, 20)):
        with pytest.raises(EN.RunRefused):
            EN.run(w["d"], src(), _cfg(w, label, lo, hi), registry=w["reg"], holdout=w["led"])
    with pytest.raises(EN.RunRefused):
        EN.run(w["d"], src(), _cfg(w, "TEST", 30, 59), registry=w["reg"], holdout=w["led"])            # TEST without TEST_OPENED
    with pytest.raises(EN.RunRefused):
        EN.run(w["d"], src(), _cfg(w, "VALIDATION", 10, 29), registry=w["reg"])                         # a VALIDATION run is recorded
    with pytest.raises(EN.RunRefused):
        EN.run(w["d"], src(), _cfg(w, "VALIDATION", 10, 29), holdout=w["led"])                          # and registered
    assert EN.run(w["d"], src(), _cfg(w, "TRAIN", 0, 9), registry=w["reg"])["identity"]["segment_check"] == "REGISTERED_SEGMENT"


def test_R02_validation_and_test_runs_over_the_full_segment_are_recorded_with_their_shas(tmp_path):
    w = _world(tmp_path)
    ds = w["d"].manifest().content_sha256
    r = EN.run(w["d"], FX.ScriptedSource(w["s"], role="STRATEGY", name="S1"), _cfg(w, "VALIDATION", 10, 29), registry=w["reg"], holdout=w["led"])
    e = w["led"].run_entry("VALIDATION_RUN", w["sha"], ds, r["identity_sha256"])
    assert e is not None and e["payload"]["result_sha256"] == r["result_sha256"] == EN.result_sha256(r) and e["payload"]["attempt"] == 1
    assert (e["payload"]["source_name"], e["payload"]["cost_multiplier"], e["payload"]["role"]) == ("S1", 1.0, "GATING")
    assert r["identity"]["segment"] == w["p"]["segments"]["VALIDATION"] and e["payload"]["benchmark_series_sha256"] == r["identity"]["benchmark"]["series_sha256"]
    w["led"].open_test(w["p"], ds)
    t = EN.run(w["d"], _src(w, "strategy"), _cfg(w, "TEST", 30, 59), registry=w["reg"], holdout=w["led"])
    assert w["led"].run_entry("TEST_RUN", w["sha"], ds, t["identity_sha256"])["payload"]["result_sha256"] == t["result_sha256"]
    assert t["identity"]["segment"] == w["p"]["segments"]["TEST"]


def test_R03_every_run_identity_records_both_chain_heads_and_one_test_result_records_the_head(tmp_path):
    w = _world(tmp_path)
    ds = w["d"].manifest().content_sha256
    before = (w["reg"].head(), w["led"].head())
    r = EN.run(w["d"], FX.ScriptedSource(w["s"], role="STRATEGY"), _cfg(w, "VALIDATION", 10, 29), registry=w["reg"], holdout=w["led"])
    assert (r["identity"]["registry_head_sha256"], r["identity"]["holdout_head_sha256"]) == before and before[0] and before[1]
    assert w["led"].head() != before[1] and w["led"].contains(before[1]) and not w["led"].contains("0" * 64)
    op = w["led"].open_test(w["p"], ds)
    head = w["led"].head()
    tr = w["led"].record_test_result(w["sha"], ds, {"strategy": r["identity_sha256"]}, "INCONCLUSIVE", op["seq"])
    assert tr["payload"]["holdout_head_sha256"] == head and tr["payload"]["test_opened_seq"] == op["seq"]
    with pytest.raises(RG.HoldoutRefused):
        w["led"].record_test_result(w["sha"], ds, {"strategy": r["identity_sha256"]}, "ESTABLISHED", op["seq"])    # one TEST_RESULT per TEST_OPENED
    syn = FX.simple_double({"SIM:X": FX.flat(5)}, n=5)
    plain = EN.run(syn, FX.ScriptedSource(syn.sessions("0", "9")), FX.v1_config(syn))["identity"]
    assert plain["segment_check"] == "SYNTHETIC_EXEMPT_UNSEGMENTED" and plain["holdout_head_sha256"] is None and plain["benchmark"] is None


def test_R04_a_historical_run_needs_a_registered_pre_registration_and_a_labelled_segment(tmp_path):
    d = FX.simple_double({"SIM:X": FX.flat(5)}, n=5, kind="HISTORICAL", licence=CONFIRMED)
    s = d.sessions("0", "9")
    p = _prereg(dataset_sha256=d.manifest().content_sha256, segments={"TRAIN": ["2023-01-02", "2023-12-29"], "VALIDATION": [s[0], s[4]],
                                                                       "TEST": ["2024-06-03", "2024-12-31"]}, benchmark_instrument="SIM:X")
    reg, led = RG.TrialRegistry(tmp_path / "t.jsonl"), RG.HoldoutLedger(tmp_path / "h.jsonl")
    reg.register(p)
    led.record_prereg(p)
    for kw, cfg in (({}, FX.v1_config(d)), ({"registry": reg}, FX.v1_config(d, prereg_sha256=PR.validate(p))),
                    ({"registry": reg, "holdout": led}, FX.v1_config(d, prereg_sha256=PR.validate(p), segment_label="TRAIN"))):
        with pytest.raises(EN.RunRefused):
            EN.run(d, FX.ScriptedSource(s), cfg, **kw)
    with pytest.raises(RGATE.AdmissionRefused) as e:                                        # I-1B-1 (ID-3, ID-8): a HISTORICAL double is never admitted
        EN.run(d, FX.ScriptedSource(s), FX.v1_config(d, prereg_sha256=PR.validate(p), segment_label="VALIDATION"), registry=reg, holdout=led)
    assert e.value.code == "DOUBLE_HISTORICAL_NOT_ADMITTED" and EN.licence_gate(d.manifest()) == "CONFIRMED"
    assert not [x for x in led.entries() if x["kind"] == "VALIDATION_RUN"]


def _src(w, which, extra=ID_EXTRA):
    return FX.ScriptedSource(w["s"], role="STRATEGY", name=w["p"][which]["id"], identity_extra=extra)


def _test_runs(w):
    return sum(1 for e in w["led"].entries() if e["kind"] == "TEST_RUN")


def test_R05_one_test_attempt_per_source_and_cost_level(tmp_path):
    w = _world(tmp_path)
    w["led"].open_test(w["p"], w["d"].manifest().content_sha256)
    kw = {"registry": w["reg"], "holdout": w["led"]}
    EN.run(w["d"], _src(w, "strategy"), _cfg(w, "TEST", 30, 59), **kw)
    with pytest.raises(EN.RunRefused, match="one TEST attempt"):
        EN.run(w["d"], _src(w, "strategy"), _cfg(w, "TEST", 30, 59), **kw)
    EN.run(w["d"], _src(w, "strategy"), _cfg(w, "TEST", 30, 59, mult=2.0), **kw)             # the other pre-registered cost level
    EN.run(w["d"], _src(w, "control"), _cfg(w, "TEST", 30, 59), **kw)                        # the other pre-registered source
    with pytest.raises(EN.RunRefused, match="one TEST attempt"):
        EN.run(w["d"], _src(w, "strategy"), _cfg(w, "TEST", 30, 59, mult=2.0), **kw)
    assert _test_runs(w) == 3


def test_R06_a_misconfigured_or_foreign_test_run_is_refused_before_it_spends_the_attempt(tmp_path):
    w = _world(tmp_path)
    w["led"].open_test(w["p"], w["d"].manifest().content_sha256)
    kw = {"registry": w["reg"], "holdout": w["led"]}
    zero = CostModel(buckets=ZERO_COST_BUCKETS, multiplier=1.0, fees=False, label="ZERO_COST")
    for over in ({"embargo_sessions": 3}, {"cost_model": zero}, {"cash_rate": CR.hypothetical(0.02)}, {"initial_cash": 20_000.0},
                 {"delisting_mode": "LAST_PRICE"}, {"static_map": {"SIM:X": {"sector": "TECH"}}}):
        calls = len(w["d"].calls)
        with pytest.raises(EN.RunRefused, match="settings differ"):
            EN.run(w["d"], _src(w, "strategy"), _cfg(w, "TEST", 30, 59, **over), **kw)
        assert _test_runs(w) == 0 and "bars" not in w["d"].calls[calls:] and "as_of" not in w["d"].calls[calls:]   # no data read, no attempt spent
    for src, mult in ((FX.ScriptedSource(w["s"], role="STRATEGY", name="FOREIGN", identity_extra=ID_EXTRA), 1.0),
                      (_src(w, "strategy", {"version": "1.0", "preregistration_hash": "0" * 64}), 1.0), (_src(w, "strategy", {}), 1.0),
                      (BuyAndHold("SIM:SPY"), 1.0), (_src(w, "control"), 2.0)):
        with pytest.raises(EN.RunRefused, match="TEST"):
            EN.run(w["d"], src, _cfg(w, "TEST", 30, 59, mult=mult), **kw)
    assert _test_runs(w) == 0
    EN.run(w["d"], _src(w, "strategy"), _cfg(w, "TEST", 30, 59), **kw)                       # the attempt is still available
    assert _test_runs(w) == 1


# ============================================================================================================ the verdict (V)
def test_V01_the_verdict_takes_no_threshold_statistic_series_or_count_and_validation_is_capped_at_inconclusive(tmp_path):
    assert list(inspect.signature(VD.verdict).parameters) == ["results", "prereg", "holdout", "registry"]
    w = _world(tmp_path)
    res, _ = _runs(w, "VALIDATION", 10, 29)
    out = _verdict(w, res)
    assert out["verdict"] == "INCONCLUSIVE" and out["reasons"] == [{"code": "ESTABLISHED_ONLY_ON_AN_OPENED_TEST_SEGMENT", "segment": "VALIDATION"}]
    ev = out["evidence"]
    assert ev["observed_mean_difference"] > 0 and ev["bootstrap_lower"] > 0 and ev["beats_control"] and ev["net_positive_at_2x"] and ev["n_sessions"] == 19
    assert res["strategy"]["total_costs"] > 0 and res["strategy_2x"]["total_costs"] > res["strategy"]["total_costs"]   # the pre-registered costs


def test_V02_established_only_on_the_opened_test_segment_and_only_once(tmp_path):
    w = _world(tmp_path)
    w["led"].open_test(w["p"], w["d"].manifest().content_sha256)
    res, _ = _runs(w, "TEST", 30, 59)
    other = _prereg(**{k: w["p"][k] for k in ("dataset_sha256", "segments", "bootstrap", "embargo_sessions", "initial_cash")},
                    thresholds={"alpha": 0.5 - 1e-9, "n_min_sessions": 1, "min_dsr": None})
    w["led"].record_prereg(other)
    with pytest.raises(VD.VerdictRefused):
        VD.verdict(res, other, w["led"], w["reg"])                                          # thresholds only from the runs' pre-registration
    out = _verdict(w, res)
    assert out["verdict"] == "ESTABLISHED" and out["holdout_flags"] == ["DATA_EXPOSURE_UNKNOWN"] and out["thresholds"] == w["p"]["thresholds"]
    tr = [e for e in w["led"].entries() if e["kind"] == "TEST_RESULT"]
    assert len(tr) == 1 and tr[0]["payload"]["verdict"] == "ESTABLISHED" and tr[0]["entry_sha256"] == out["test_result"]
    with pytest.raises(VD.VerdictRefused, match="one look, one result"):
        _verdict(w, res)


@pytest.mark.parametrize("flag", ["FIXED_CURRENT_UNIVERSE", "SPLIT_UNADJUSTED", "CORPORATE_ACTION_UNACCOUNTED", "STATIC_CLASSIFICATION_MAP"])
def test_V03_each_cap_holds_the_verdict_at_not_established(tmp_path, flag):
    w = _world(tmp_path)
    res, _ = _runs(w, "VALIDATION", 10, 29, src_flags=[flag, "STALE_MARK"])
    out = _verdict(w, res)
    assert out["verdict"] == "NOT_ESTABLISHED" and out["caps"] == [flag]


def test_V04_tampered_evidence_and_unrecorded_runs_are_refused_and_unhashed_fields_have_no_effect(tmp_path):
    w = _world(tmp_path)
    res, _ = _runs(w, "VALIDATION", 10, 29)
    ref = _verdict(w, res)
    edits = [lambda r: r["strategy"].__setitem__("identity_sha256", "0" * 64),
             lambda r: r["strategy"]["identity"]["costs"].__setitem__("multiplier", 2.0),
             lambda r: r["strategy"]["rows"][5].__setitem__("equity", r["strategy"]["rows"][5]["equity"] * 1.1),
             lambda r: (r["strategy"]["rows"][5].__setitem__("equity", 1e9), r["strategy"].__setitem__("result_sha256", EN.result_sha256(r["strategy"]))),
             lambda r: r.__setitem__("strategy_2x", r["strategy"]),
             lambda r: (r["strategy_2x"]["identity"].__setitem__("initial_cash", 1.0), r["strategy_2x"].__setitem__("identity_sha256", EN.sha256_of(r["strategy_2x"]["identity"])),
                        r["strategy_2x"].__setitem__("result_sha256", EN.result_sha256(r["strategy_2x"])))]
    for edit in edits:
        bad = copy.deepcopy(res)
        edit(bad)
        with pytest.raises(VD.VerdictRefused):
            _verdict(w, bad)
    loose = copy.deepcopy(res)
    for r in loose.values():
        r["initial_cash"], r["total_costs"], r["traded_notional"] = 1e12, 0.0, 0.0              # top-level, unhashed: read by nothing in the verdict
    assert _verdict(w, loose) == ref
    w2 = _world(tmp_path, "w2")
    res2, _ = _runs(w2, "VALIDATION", 10, 29)
    with pytest.raises(VD.VerdictRefused):
        _verdict(w, res2)                                                                   # runs bound to another world's ledger


def test_V05_a_rebuilt_truncated_ledger_is_refused_by_the_recorded_head(tmp_path):
    w = _world(tmp_path)
    res, _ = _runs(w, "VALIDATION", 10, 29)
    path = w["led"].path
    path.write_text("", encoding="utf-8")                                                   # truncated to nothing, then rebuilt with the same runs
    fake = RG.HoldoutLedger(path, clock=lambda: "2099-01-01T00:00:00Z")
    fake.record_prereg(w["p"])
    for r in res.values():
        fake.record_validation_run(w["sha"], r["identity"]["content_sha256"], r["identity_sha256"], r["result_sha256"], r["identity"]["benchmark"]["series_sha256"])
    with pytest.raises(VD.VerdictRefused, match="truncated"):
        VD.verdict(res, w["p"], fake, w["reg"])


def test_V06_the_control_is_the_pre_registrations_c1_and_the_random_control_never_enters(tmp_path):
    w = _world(tmp_path)
    for name in ("RANDOM_ENTRY_CONTROL", "SOME_OTHER_CONTROL"):
        res, _ = _runs(w, "VALIDATION", 10, 29, control_name=name)
        with pytest.raises(VD.VerdictRefused, match="random-entry|control"):
            _verdict(w, res)
    assert "beats_control means beating THIS control" in VD.__doc__ and "random-entry" in VD.__doc__


def test_V07_not_established_and_inconclusive_paths(tmp_path):
    flat = _world(tmp_path, "flat", rising=False)
    res, _ = _runs(flat, "VALIDATION", 10, 29)
    assert _verdict(flat, res)["reasons"][0]["code"] == "NO_POSITIVE_DIFFERENCE"
    big = _world(tmp_path, "nmin", thresholds={"alpha": 0.10, "n_min_sessions": 100, "min_dsr": None})
    big["led"].open_test(big["p"], big["d"].manifest().content_sha256)
    res, _ = _runs(big, "TEST", 30, 59)
    assert _verdict(big, res)["verdict"] == "INCONCLUSIVE"
    same = _world(tmp_path, "same")
    same["led"].open_test(same["p"], same["d"].manifest().content_sha256)
    res, _ = _runs(same, "TEST", 30, 59, control_entries=True)                              # the control holds the same position
    out = _verdict(same, res)
    assert out["verdict"] == "INCONCLUSIVE" and out["reasons"] == [{"code": "CRITERIA_NOT_MET", "failed": ["beats_control"]}]


@pytest.mark.parametrize("basis", ["STRATEGY_EXCESS_OVER_CASH", "EXCESS_OVER_EXPOSURE_MATCHED_SPY"])
def test_V08_the_deflated_sharpe_uses_the_declared_basis_and_the_registrys_trial_count(tmp_path, basis):
    w = _world(tmp_path, basis, thresholds={"alpha": 0.10, "n_min_sessions": 10, "min_dsr": 0.5}, spy_rising=True, dsr_basis=basis)
    w["led"].open_test(w["p"], w["d"].manifest().content_sha256)                            # k = 1 registered + 30 prior variants (exposure UNKNOWN)
    res, spy = _runs(w, "TEST", 30, 59)
    out = _verdict(w, res)
    s = res["strategy"]
    rs = [b / a - 1.0 for a, b in zip([r["equity"] for r in s["rows"]], [r["equity"] for r in s["rows"]][1:])]
    cash_excess = [x - r["cash_return"] for x, r in zip(rs, s["rows"][1:])]
    em = BM.exposure_matched(s, spy)["returns"]
    vs_spy = [x - e for x, e in zip(rs, em[1:])]
    want = IN.dsr(cash_excess if basis == "STRATEGY_EXCESS_OVER_CASH" else vs_spy, 31)
    got = out["evidence"]["dsr"]
    assert got["basis"] == basis and got["n_trials"] == 31 and abs(got["sr"] - want["sr"]) <= 1e-12 and abs(got["dsr"] - want["dsr"]) <= 1e-12
    assert IN.moments(cash_excess)["sr"] != IN.moments(vs_spy)["sr"]                         # the two bases differ in this world
    with pytest.raises(ValueError):
        IN.dsr([0.01, 0.02, 0.0, 0.03], 5, trial_sharpes=[2.0, 0.1])                         # LOW-8: an annualised figure is refused


def test_V09_a_substituted_benchmark_series_is_refused(tmp_path):
    w = _world(tmp_path)
    res, spy = _runs(w, "VALIDATION", 10, 29)
    sessions = [r["session"] for r in res["strategy"]["rows"]]
    assert spy == BM.total_return_series(RGATE.admit_double(w["d"]), "SIM:SPY", sessions) and res["strategy"]["identity"]["benchmark"]["instrument_id"] == "SIM:SPY"
    assert all(res[k]["identity"]["benchmark"] == res["strategy"]["identity"]["benchmark"] for k in res)       # same provider, same sessions
    fake = [0.0] + [0.01] * (len(sessions) - 1)

    def swap(r, rehash_result=False, rehash_identity=False):
        for role in r:
            r[role]["benchmark_returns"] = list(fake)
            if rehash_identity:
                r[role]["identity"]["benchmark"]["series_sha256"] = BM.series_sha256(fake)
                r[role]["identity_sha256"] = EN.sha256_of(r[role]["identity"])
            if rehash_result or rehash_identity:
                r[role]["result_sha256"] = EN.result_sha256(r[role])
    for kw in ({}, {"rehash_result": True}, {"rehash_identity": True}):
        bad = copy.deepcopy(res)
        swap(bad, **kw)
        with pytest.raises(VD.VerdictRefused):
            _verdict(w, bad)
    assert _verdict(w, res)["verdict"] == "INCONCLUSIVE"                                     # the untouched runs still decide


def test_V10_the_engine_uses_only_the_pre_registered_benchmark_instrument(tmp_path):
    with pytest.raises(PR.PreregError):
        PR.validate(_prereg(benchmark_instrument="<instrument_id of SPY in the dataset>"))
    with pytest.raises(PR.PreregError):
        PR.validate({k: v for k, v in _prereg().items() if k != "benchmark_instrument"})
    assert PR.validate(_prereg(benchmark_instrument="SIM:X")) != PR.validate(_prereg())                    # part of the sha
    w = _world(tmp_path, "x", benchmark="SIM:X")
    res, _ = _runs(w, "VALIDATION", 10, 29)
    assert {res[k]["identity"]["benchmark"]["instrument_id"] for k in res} == {"SIM:X"}                   # not SPY, though SPY is in the dataset
    gone = _world(tmp_path, "gone", benchmark="SIM:GONE")
    with pytest.raises(EN.RunRefused, match="benchmark"):
        _runs(gone, "VALIDATION", 10, 29)


def test_V11_a_missing_total_return_view_is_unavailable_and_the_verdict_refuses(tmp_path):
    w = _world(tmp_path, views=("RAW", "SPLIT_ADJUSTED"))
    res, spy = _runs(w, "VALIDATION", 10, 29)
    assert spy == {"status": "UNAVAILABLE", "reason": "TOTAL_RETURN_VIEW_UNAVAILABLE"} and res["strategy"]["identity"]["benchmark"]["status"] == "UNAVAILABLE"
    assert BM.total_return_series(RGATE.admit_double(w["d"]), "SIM:SPY", w["s"][10:30]).status == "UNAVAILABLE"
    with pytest.raises(VD.VerdictRefused, match="UNAVAILABLE"):
        _verdict(w, res)


def test_V12_a_sub_range_of_the_declared_segment_is_refused(tmp_path):
    w = _world(tmp_path)
    res, _ = _runs(w, "VALIDATION", 12, 29)                                                 # recorded, but not the declared segment
    assert w["led"].run_entry("VALIDATION_RUN", w["sha"], w["d"].manifest().content_sha256, res["strategy"]["identity_sha256"]) is not None
    with pytest.raises(VD.VerdictRefused, match="segment"):
        _verdict(w, res)


def test_V13_settings_must_equal_the_pre_registration_and_each_other(tmp_path):
    w = _world(tmp_path)
    zero = CostModel(buckets=ZERO_COST_BUCKETS, multiplier=2.0, fees=False, label="ZERO_COST")
    c = w["p"]["costs"]
    inflated = CostModel(buckets=[dict(b, slippage_bps=b["slippage_bps"] * 2) for b in c["buckets"]], multiplier=1.0, fees=c["fees"], label=c["label"])
    hyp = CR.hypothetical(0.02)
    cases = [({"strategy_2x": {"cost_model": zero}}, "zero-cost 2x"), ({"control": {"mult": 2.0}}, "control at 2x"),
             ({"control": {"cost_model": inflated}}, "inflated control buckets"), ({"strategy": {"embargo_sessions": 3}}, "changed embargo"),
             ({r: {"embargo_sessions": 3} for r in ("strategy", "strategy_2x", "control")}, "embargo unlike the pre-registration"),
             ({r: {"cash_rate": hyp} for r in ("strategy", "strategy_2x", "control")}, "cash mode"),
             ({r: {"delisting_mode": "LAST_PRICE"} for r in ("strategy", "strategy_2x", "control")}, "delisting sensitivity"),
             ({r: {"initial_cash": 20_000.0} for r in ("strategy", "strategy_2x", "control")}, "initial cash")]
    for over, why in cases:
        res, _ = _runs(w, "VALIDATION", 10, 29, over=over)
        with pytest.raises(VD.VerdictRefused):
            _verdict(w, res)
    res, _ = _runs(w, "VALIDATION", 10, 29)
    assert _verdict(w, res)["verdict"] == "INCONCLUSIVE"                                     # the pre-registered settings are accepted


def test_V14_the_strategy_and_control_identity_is_required_not_only_the_name(tmp_path):
    w = _world(tmp_path)
    for kw in ({"strat_extra": {}}, {"strat_extra": {"version": "1.0", "preregistration_hash": "0" * 64}}, {"strat_extra": {"preregistration_hash": V1_HASH}},
               {"control_extra": {}}, {"control_extra": {"version": "2.0", "preregistration_hash": V1_HASH}}, {"control_extra": {"version": "1.0"}}):
        res, _ = _runs(w, "VALIDATION", 10, 29, **kw)
        with pytest.raises(VD.VerdictRefused, match="strategy|control"):
            _verdict(w, res)
    v1, c1 = SwingBridge(True, FX.UNIVERSE).identity(), SwingBridge(False, FX.UNIVERSE).identity()
    assert (v1["name"], v1["version"], v1["preregistration_hash"]) == ("SWING_TREND_PULLBACK", "1.0", V1_HASH)
    assert (c1["name"], c1["version"], c1["preregistration_hash"]) == ("SWING_TREND_PULLBACK_CONTROL", "1.0", V1_HASH)   # what the real runs carry


def test_V15_the_trial_registry_is_bound_and_must_hold_the_reconstructed_priors(tmp_path):
    bare = _world(tmp_path, "bare", priors=False)
    res, _ = _runs(bare, "VALIDATION", 10, 29)
    assert bare["reg"].trial_count(bare["d"].manifest().content_sha256, "2024-01-01", "2024-12-31", True)["k"] == 1
    with pytest.raises(VD.VerdictRefused, match="prior trials"):
        _verdict(bare, res)                                                                   # a fresh registry (k = 1) is refused
    w = _world(tmp_path)
    res, _ = _runs(w, "VALIDATION", 10, 29)
    other = _world(tmp_path, "other")                                                         # the same pre-registration, another chain
    assert other["reg"].trial_entry(w["sha"]) is not None and other["reg"].has_reconstructed_priors()
    with pytest.raises(VD.VerdictRefused, match="registry head"):
        VD.verdict(res, w["p"], w["led"], other["reg"])
    assert _verdict(w, res)["verdict"] == "INCONCLUSIVE"


def test_V16_a_hypothetical_cash_rate_is_pinned_exactly(tmp_path):
    w = _world(tmp_path, cash_rate={"mode": "HYPOTHETICAL_CONSTANT", "annual": 0.02})
    res, _ = _runs(w, "VALIDATION", 10, 29)
    assert res["strategy"]["identity"]["cash_rate"]["annual"] == 0.02 and _verdict(w, res)["verdict"] == "INCONCLUSIVE"
    res, _ = _runs(w, "VALIDATION", 10, 29, over={r: {"cash_rate": CR.hypothetical(0.03)} for r in ("strategy", "strategy_2x", "control")})
    with pytest.raises(VD.VerdictRefused, match="cash_rate.annual"):
        _verdict(w, res)
    p = _prereg()
    for bad in ({"mode": "HYPOTHETICAL_CONSTANT"}, {"mode": "HYPOTHETICAL_CONSTANT", "annual": 2}, {"mode": "REGISTERED_SERIES"},
                {"mode": "REGISTERED_SERIES", "sha256": "xyz"}, {"mode": "ZERO", "annual": 0.0}):
        with pytest.raises(PR.PreregError):
            PR.validate(dict(p, cash_rate=bad))
    with pytest.raises(PR.PreregError, match="not registered"):
        PR.validate(dict(p, cash_rate={"mode": "REGISTERED_SERIES", "sha256": "a" * 64}))    # LOW-2: well-formed but not registered
    st ={"cash_rate": {"mode": "REGISTERED_SERIES", "annual": 0.0, "sha256": "b" * 64}}
    assert "cash_rate.sha256" in PR.settings_mismatch(st, dict(p, cash_rate={"mode": "REGISTERED_SERIES", "sha256": "a" * 64}))


def test_V17_to_be_accepted_trials_count_toward_k_and_the_judged_trial_must_be_counted(tmp_path, monkeypatch):
    reg = RG.TrialRegistry(tmp_path / "t.jsonl")
    reg.register(_prereg(hypothesis_id="H-TBA", family=["H-TBA"], dataset_sha256=PR.template()["dataset_sha256"]))   # the template default
    reg.register(_prereg(hypothesis_id="H-OTHER", family=["H-OTHER"], dataset_sha256="e" * 64))                      # a named other dataset
    reg.register(_prereg())
    tc = reg.trial_count(DATA, "2016-01-04", "2019-12-31", data_exposure_unknown=False)
    assert PR.template()["dataset_sha256"] == "TO_BE_ACCEPTED" and tc["k"] == 2 and sorted(tc["counted"]) == ["H-TBA", "H-V1"]
    w = _world(tmp_path, "tba", dataset="TO_BE_ACCEPTED", thresholds={"alpha": 0.10, "n_min_sessions": 10, "min_dsr": 0.5})
    res, _ = _runs(w, "VALIDATION", 10, 29)
    assert _verdict(w, res)["evidence"]["dsr"]["n_trials"] == 31                            # 30 prior variants + the TO_BE_ACCEPTED trial itself

    def exact_only(self, dataset_sha256, start, end, data_exposure_unknown):                 # control: the old, optimistic count
        es = [e["payload"] for e in self.entries() if e["kind"] == "TRIAL_REGISTERED" and e["payload"]["dataset_sha256"] == dataset_sha256]
        return {"k": max(len(es), 1), "counted": [p["hypothesis_id"] for p in es], "count_is_lower_bound": True}
    monkeypatch.setattr(RG.TrialRegistry, "trial_count", exact_only)
    with pytest.raises(VD.VerdictRefused, match="not counted"):
        _verdict(w, res)


def test_R07_a_run_over_another_pre_registrations_unopened_test_range_is_refused(tmp_path):
    d = FX.simple_double({"SIM:X": FX.flat(10)}, n=10)
    s, ds = d.sessions("0", "9"), d.manifest().content_sha256
    p1 = _prereg(hypothesis_id="H-P1", family=["H-P1"], dataset_sha256=ds, benchmark_instrument="SIM:X",
                 segments={"TRAIN": ["2023-01-02", "2023-06-30"], "VALIDATION": ["2023-07-03", "2023-12-29"], "TEST": [s[5], s[9]]})
    p2 = _prereg(hypothesis_id="H-P2", family=["H-P2"], dataset_sha256=ds, benchmark_instrument="SIM:X",
                 segments={"TRAIN": [s[0], s[9]], "VALIDATION": ["2024-02-01", "2024-06-28"], "TEST": ["2024-07-01", "2024-12-31"]})
    reg, led = RG.TrialRegistry(tmp_path / "t.jsonl"), RG.HoldoutLedger(tmp_path / "h.jsonl")
    for p in (p1, p2):
        reg.register(p)
        led.record_prereg(p)
    cfg = FX.v1_config(d, prereg_sha256=PR.validate(p2), segment_label="TRAIN")
    with pytest.raises(EN.RunRefused, match="OVERLAPS_UNOPENED_TEST_OF_OTHER_PREREG.*H-P1"):
        EN.run(d, FX.ScriptedSource(s), cfg, registry=reg, holdout=led)
    with pytest.raises(EN.RunRefused, match="OVERLAPS_UNOPENED_TEST_OF_OTHER_PREREG"):
        EN.run(d, FX.ScriptedSource(s), cfg, registry=reg)                                   # no ledger to prove it opened: fail closed
    led.open_test(p1, ds)
    assert EN.run(d, FX.ScriptedSource(s), cfg, registry=reg, holdout=led)["identity"]["segment_check"] == "REGISTERED_SEGMENT"
    with pytest.raises(EN.RunRefused, match="mislabelled"):
        EN.run(d, FX.ScriptedSource(s), FX.v1_config(d, prereg_sha256=PR.validate(p2), segment_label="VALIDATION"), registry=reg, holdout=led)
    p3 = _prereg(hypothesis_id="H-P3", family=["H-P3"], dataset_sha256=ds, benchmark_instrument="SIM:X",
                 segments={"TRAIN": ["2023-01-02", "2023-06-30"], "VALIDATION": ["2024-08-01", "2024-12-31"], "TEST": ["2025-01-02", "2025-06-30"]})
    reg2 = RG.TrialRegistry(tmp_path / "t2.jsonl")
    reg2.register(p3)
    reg2.register(p2)
    assert EN.run(d, FX.ScriptedSource(s), cfg, registry=reg2)["identity"]["segment_check"] == "REGISTERED_SEGMENT"   # disjoint ranges pass


def test_V18_the_control_must_share_the_strategys_universe_and_static_map(tmp_path):
    w = _world(tmp_path)
    drop = lambda ident: {k: v for k, v in ident.items() if k != "name"}  # noqa: E731
    v1 = drop(SwingBridge(True, FX.UNIVERSE).identity())
    for ctrl, field in ((drop(SwingBridge(False, "TINY_UNIVERSE").identity()), "universe_id"),
                        (dict(drop(SwingBridge(False, FX.UNIVERSE).identity()), static_map_sha256="a" * 64), "static_map_sha256")):
        res, _ = _runs(w, "VALIDATION", 10, 29, strat_extra=v1, control_extra=ctrl)
        with pytest.raises(VD.VerdictRefused, match=field):
            _verdict(w, res)
    res, _ = _runs(w, "VALIDATION", 10, 29, strat_extra=v1, control_extra=drop(SwingBridge(False, FX.UNIVERSE).identity()))
    assert _verdict(w, res)["verdict"] == "INCONCLUSIVE"                                     # the same universe and map: accepted


# ============================================================================================================ the licence rule (LC)
@pytest.mark.parametrize("licence", [None, {"personal_research_use": "NOT_PERMITTED"},
                                     {k: v for k, v in CONFIRMED.items() if k != "licence_file_sha256"}])
def test_LC01_historical_data_without_a_confirmed_licence_is_refused_before_any_data_call(licence):
    d = FX.simple_double({"SIM:X": FX.flat(5)}, n=5, kind="HISTORICAL", licence=licence)
    with pytest.raises(LicenceRefused):
        EN.run(d, FX.ScriptedSource(d.sessions("0", "9")), FX.v1_config(d))
    assert d.calls == ["manifest"]


def test_LC02_synthetic_is_exempt_and_labelled_and_confirmed_historical_still_needs_its_registration():
    syn = FX.simple_double({"SIM:X": FX.flat(5)}, n=5)
    ident = EN.run(syn, FX.ScriptedSource(syn.sessions("0", "9")), FX.v1_config(syn))["identity"]
    assert ident["licence"] == "SYNTHETIC_EXEMPT" and ident["segment_check"] == "SYNTHETIC_EXEMPT_UNSEGMENTED"
    d = FX.simple_double({"SIM:X": FX.flat(5)}, n=5, kind="HISTORICAL", licence=CONFIRMED)
    with pytest.raises(EN.RunRefused):
        EN.run(d, FX.ScriptedSource(d.sessions("0", "9")), FX.v1_config(d))
    assert d.calls == ["manifest"]


def test_LC03_total_return_series_shares_the_licence_gate():
    for lic in (None, {"personal_research_use": "NOT_PERMITTED"}):
        d = FX.simple_double({"SIM:SPY": FX.flat(5)}, n=5, kind="HISTORICAL", licence=lic)
        with pytest.raises(LicenceRefused):
            BM.total_return_series(d, "SIM:SPY", d.sessions("0", "9"))
        assert d.calls == ["manifest"]
    ok = FX.simple_double({"SIM:SPY": FX.flat(5)}, n=5, kind="HISTORICAL", licence=CONFIRMED)
    assert EN.licence_gate(ok.manifest()) == "CONFIRMED"                                    # I-1B-1: the licence gate passes; the admission gate refuses
    n = len(ok.calls)
    with pytest.raises(RGATE.NotAdmitted) as e:
        BM.total_return_series(ok, "SIM:SPY", ok.sessions("0", "9"))
    assert e.value.code == "NOT_ADMITTED" and ok.calls[n:] == ["manifest"]


def test_LC04_the_signal_bridge_refuses_a_historical_view_without_a_confirmed_licence():
    for lic, refused in ((None, True), (CONFIRMED, False)):
        d = FX.v1_double(kind="HISTORICAL", licence=lic)
        s = d.sessions("0", "9")
        view = d.as_of(d.session_times(s[210])[1])
        ctx = DecisionContext(view, s[210], BookSnapshot(s[210], 1e5, 1e5, False, ()))
        bridge = SwingBridge(True, FX.UNIVERSE)
        if refused:
            with pytest.raises(LicenceRefused):
                bridge.entries(ctx)
            with pytest.raises(LicenceRefused):
                bridge.dataset(view, s[210])
        else:
            assert EN.licence_gate(view.manifest()) == "CONFIRMED"                         # I-1B-1: no admitted HISTORICAL view exists (ID-8)
            for call in (lambda: bridge.entries(ctx), lambda: bridge.dataset(view, s[210])):
                with pytest.raises(RGATE.NotAdmitted) as e:
                    call()
                assert e.value.code == "NOT_ADMITTED_VIEW"


def test_LC05_the_random_control_refuses_unregistered_historical_data_and_the_test_segment(tmp_path):
    h = FX.simple_double({"SIM:X": FX.flat(5)}, n=5, kind="HISTORICAL", licence=CONFIRMED)
    with pytest.raises(EN.RunRefused):
        BM.random_control(h, FX.v1_config(h), FX.UNIVERSE, [1])
    with pytest.raises(EN.RunRefused):
        BM.random_control(h, FX.v1_config(h, segment_label="VALIDATION"), FX.UNIVERSE, [1])        # labelled, but no registry / ledger
    u = FX.simple_double({"SIM:X": FX.flat(5)}, n=5, kind="HISTORICAL")
    with pytest.raises(LicenceRefused):
        BM.random_control(u, FX.v1_config(u), FX.UNIVERSE, [1])
    syn = FX.simple_double({"SIM:X": FX.flat(5)}, n=5)
    with pytest.raises(EN.RunRefused):
        BM.random_control(syn, FX.v1_config(syn, segment_label="TEST"), FX.UNIVERSE, [1])


@pytest.mark.parametrize("kind", ["SYNTHETIC", "HISTORICAL"])
def test_LC06_the_random_control_runs_as_a_registered_recorded_validation_run(tmp_path, kind):
    d = FX.v1_double(kind=kind, licence=CONFIRMED if kind == "HISTORICAL" else None)
    s = d.sessions("0", "9")
    p = _prereg(dataset_sha256=d.manifest().content_sha256, segments={"TRAIN": [s[0], s[199]], "VALIDATION": [s[200], s[239]], "TEST": [s[240], s[259]]},
                benchmark_instrument=FX.SPY)
    reg, led = RG.TrialRegistry(tmp_path / "t.jsonl"), RG.HoldoutLedger(tmp_path / "h.jsonl")
    reg.register(p)
    led.record_prereg(p)
    cfg = EN.RunConfig(start_session=s[200], end_session=s[239], prereg_sha256=PR.validate(p), segment_label="VALIDATION")
    if kind == "HISTORICAL":                                                                # I-1B-1 (ID-3, ID-8): refused; nothing is recorded
        assert EN.licence_gate(d.manifest()) == "CONFIRMED"
        with pytest.raises(RGATE.NotAdmitted) as e:
            BM.random_control(d, cfg, FX.UNIVERSE, [3, 4], registry=reg, holdout=led)
        assert e.value.code == "NOT_ADMITTED"
        with pytest.raises(RGATE.AdmissionRefused) as e:
            RGATE.admit_double(d)
        assert e.value.code == "DOUBLE_HISTORICAL_NOT_ADMITTED" and not [x for x in led.entries() if x["kind"] == "VALIDATION_RUN"]
        return
    out = BM.random_control(RGATE.admit_double(d), cfg, FX.UNIVERSE, [3, 4], registry=reg, holdout=led)
    rec = [e["payload"] for e in led.entries() if e["kind"] == "VALIDATION_RUN"]
    assert [r["role"] for r in rec] == ["RANDOM_CONTROL", "RANDOM_CONTROL"] and [r["identity_sha256"] for r in rec] == [x["identity_sha256"] for x in out["runs"]]
    assert out["seeds"] == [3, 4] and out["role"] == "REPORT_ONLY_NEVER_TUNES_V1"


# ============================================================================================================ isolation (X)
class _Reaches(SignalSource):
    role = "BENCHMARK"

    def __init__(self, what):
        self.what = what

    def entries(self, ctx):
        if self.what == "socket":
            socket.socket()
        else:
            sqlite3.connect(":memory:")
        return []


@pytest.mark.parametrize("what", ["socket", "sqlite"])
def test_X01_no_socket_and_no_sqlite_during_a_run_and_both_are_restored_after(what):
    d = FX.simple_double({"SIM:X": FX.flat(4)}, n=4)
    with pytest.raises(IsolationViolation):
        EN.run(d, _Reaches(what), FX.v1_config(d))
    con = sqlite3.connect(":memory:")
    con.close()


ALLOWED = ("__future__", "_socket", "_sqlite3", "contextlib", "copy", "dataclasses", "datetime", "hashlib", "json", "math", "pathlib", "random",
           "socket", "sqlite3", "statistics", "types", "typing", "md_eval", "research_gate", "swing_pullback", "strategy_validation.dataset",
           "strategy_validation.models", "rotation_backtest.metrics")


def test_X02_md_eval_imports_only_the_standard_library_and_the_reused_modules():
    for f in sorted((ROOT / "md_eval").glob("*.py")):
        tree = ast.parse(f.read_text(encoding="utf-8"))
        mods = {n.module or "" for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)} | {a.name for n in ast.walk(tree) if isinstance(n, ast.Import)
                                                                                         for a in n.names}
        bad = {m for m in mods if not any(m == q or m.startswith(q + ".") for q in ALLOWED)}
        assert not bad, (f.name, bad)
        assert not {m for m in mods if m.startswith("swing_pullback.") and m not in ("swing_pullback.core",)}, f.name


def test_X03_md_eval_never_names_the_evaluator_or_the_research_data_package_and_nothing_outside_imports_it():
    for f in sorted((ROOT / "md_eval").glob("*.py")):
        t = f.read_text(encoding="utf-8")
        assert "swing_eval" not in t and "research_data" not in t, f.name
    for p in ROOT.rglob("*.py"):
        rel = p.relative_to(ROOT).as_posix()
        if rel.startswith(("md_eval/", "research_gate/", "tests/", ".venv/", "scratchpad/", ".claude/")) or "/site-packages/" in rel:
            continue
        assert "md_eval" not in p.read_text(encoding="utf-8", errors="replace"), rel
