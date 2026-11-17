"""md_eval/verdict.py - ESTABLISHED / INCONCLUSIVE / NOT_ESTABLISHED, default NOT_ESTABLISHED.

verdict(results, prereg, holdout, registry) takes NO threshold, NO caller-assembled statistic, NO caller-supplied benchmark series and NO trial
count: every input it uses is read from the pre-registration, the two ledgers, or hashed identity / result fields of three engine runs:
  results["strategy"]     the pre-registration's strategy at cost level 1x
  results["strategy_2x"]  the same strategy at cost level 2x (the net-positive-at-2x criterion)
  results["control"]      the pre-registration's CONTROL (C1 for V1) at 1x. beats_control means beating THIS control; the random-entry
                          control is report-only and is refused here (LOW-6)

Binding to the LEDGER (MED-1 d, LOW-1): each run's identity_sha256 and result_sha256 are recomputed and must equal a VALIDATION_RUN / TEST_RUN entry
for this pre-registration and dataset, with the same benchmark series sha256; the chain head the run recorded must still be in the chain.
Binding to the PRE-REGISTRATION (MED-3):
  - the run's segment (start, end, label) EQUALS the declared segment of that label (a sub-range is refused);
  - the settings equal the pre-registration's and are identical across the three runs: cost buckets, fees on / off, cost label, embargo, cash
    mode, delisting mode PRIMARY, initial cash, static map sha256 (or none), the frozen book; the cost multiplier 1x / 2x / 1x by role;
  - the strategy carries the pre-registration's preregistration_hash and version; the control its id, version and the same frozen hash (V1 and
    C1 share one pre-registration hash); a name alone is refused; the control's universe_id and static_map_sha256 equal the strategy's (LOW-1);
  - the trial being judged is counted in the registry's trial count (MED-2);
  - on TEST: TEST_OPENED after PREREG_RECORDED, and exactly one TEST_RESULT per TEST_OPENED (recorded here; a second verdict is refused);
  - the benchmark is the pre-registered benchmark_instrument's total-return series computed by the engine; UNAVAILABLE refuses.
The evidence is computed here: the daily differences against exposure-matched SPY on the bound series, the stationary bootstrap with the
pre-registered block, resamples, seed and confidence, the 2x net result against the identity's initial cash, the mean daily excess over the
control, and (min_dsr set) the deflated Sharpe on the pre-registered dsr_basis with k = the trial registry's conservative count (exposure
treated as UNKNOWN, so every reconstructed prior counts).
Binding to the TRIAL REGISTRY (LOW-R1): the registry presented contains the registry head each run recorded (the run-time registry is a prefix
of it) and every reconstructed prior trial, so k cannot be understated by presenting a fresh registry.
What is NOT verified: a source's identity (name, version, preregistration_hash) is DECLARED by the source object, recorded in the hashed run
identity and limited to one TEST attempt per source and cost level; the code that produced the signals is not itself hashed or checked here.
Then, in order:
  1. a missing input -> NOT_ESTABLISHED (MISSING_INPUT)
  2. caps -> NOT_ESTABLISHED: FIXED_CURRENT_UNIVERSE, SPLIT_UNADJUSTED (§7), CORPORATE_ACTION_UNACCOUNTED (A7), STATIC_CLASSIFICATION_MAP (A4)
  3. observed mean difference <= 0, or not net positive at the 2x cost level -> NOT_ESTABLISHED
  4. fewer sessions than n_min_sessions -> INCONCLUSIVE
  5. p <= alpha, the bootstrap lower bound > 0, the control beaten, and (min_dsr set) DSR >= min_dsr -> ESTABLISHED, but ONLY on a TEST segment
     with a TEST_OPENED entry recorded after its PREREG_RECORDED (MED-1 a); any other segment is capped at INCONCLUSIVE."""
from __future__ import annotations

import math

from md_eval import BOOK, VERDICT_CAPS
from md_eval import benchmarks as BM
from md_eval import engine as EN
from md_eval import inference as IN
from md_eval import metrics as MT
from md_eval import prereg as PR
from md_eval.contract import LicenceRefused

VERDICTS = ("ESTABLISHED", "INCONCLUSIVE", "NOT_ESTABLISHED")
ROLES = {"strategy": 1.0, "strategy_2x": 2.0, "control": 1.0}
RANDOM_CONTROL_PREFIX = "RANDOM_ENTRY_CONTROL"
CASH_MODE_OF = {"ZERO": "ZERO", "HYPOTHETICAL_CONSTANT": "HYPOTHETICAL_CONSTANT", "REGISTERED_SERIES": "REGISTERED_SERIES"}


class VerdictRefused(RuntimeError):
    pass


def _bind(role: str, r: dict, sha: str, holdout) -> dict:
    ident = r.get("identity") or {}
    if EN.sha256_of(ident) != r.get("identity_sha256"):
        raise VerdictRefused(f"{role}: the identity does not hash to its identity_sha256 (edited)")
    if EN.result_sha256(r) != r.get("result_sha256"):
        raise VerdictRefused(f"{role}: the result content does not hash to its result_sha256 (edited evidence)")
    if ident.get("prereg_sha256") != sha:
        raise VerdictRefused(f"{role}: the run names another pre-registration")
    kind = {"VALIDATION": "VALIDATION_RUN", "TEST": "TEST_RUN"}.get(ident.get("segment_label"))
    if kind is None:
        raise VerdictRefused(f"{role}: only a recorded VALIDATION or TEST run can be judged")
    e = holdout.run_entry(kind, sha, ident.get("content_sha256"), r["identity_sha256"])
    if e is None or e["payload"]["result_sha256"] != r["result_sha256"]:
        raise VerdictRefused(f"{role}: no {kind} entry in the holdout ledger binds this run (identity and result sha256)")
    bench = ident.get("benchmark") or {}
    if not bench or e["payload"].get("benchmark_series_sha256") != bench.get("series_sha256") or BM.series_sha256(r.get("benchmark_returns")) != bench.get("series_sha256"):
        raise VerdictRefused(f"{role}: the benchmark series is not the one bound to this run (identity and ledger series sha256)")
    if not holdout.contains(ident.get("holdout_head_sha256")):
        raise VerdictRefused(f"{role}: the ledger head this run recorded is no longer in the chain (truncated)")
    name = str((ident.get("source") or {}).get("name", ""))
    if name.startswith(RANDOM_CONTROL_PREFIX):
        raise VerdictRefused("the random-entry control is report-only and never enters the verdict")
    if float((ident.get("costs") or {}).get("multiplier", -1)) != ROLES[role]:
        raise VerdictRefused(f"{role}: cost level {ROLES[role]}x expected")
    return ident


def _settings(ident: dict) -> dict:
    c, cr = ident.get("costs") or {}, ident.get("cash_rate") or {}
    return {"buckets": c.get("buckets"), "fees": c.get("fees"), "label": c.get("label"), "embargo_sessions": ident.get("embargo_sessions"),
            "cash_rate": {k: cr.get(k) for k in ("mode", "annual", "sha256")}, "delisting_mode": ident.get("delisting_mode"),
            "initial_cash": ident.get("initial_cash"), "static_map_sha256": ident.get("static_map_sha256"), "book": ident.get("book"),
            "segment": ident.get("segment"), "segment_label": ident.get("segment_label"), "content_sha256": ident.get("content_sha256"),
            "kind": ident.get("kind"), "licence": ident.get("licence"), "benchmark": ident.get("benchmark")}


def _against_prereg(st: dict, p: dict) -> list:
    """The settings that differ from the pre-registration (MED-3, LOW-R2): the same check the engine's TEST guard applies before a run."""
    return PR.settings_mismatch(st, p)


def verdict(results: dict, prereg: dict, holdout, registry) -> dict:
    sha = PR.validate(prereg)
    rec = holdout.prereg_entry(sha)
    if rec is None:
        raise VerdictRefused("the pre-registration's sha256 is not recorded in the holdout ledger: thresholds must be recorded before the runs")
    if registry.trial_entry(sha) is None:
        raise VerdictRefused("the pre-registration is not registered in the trial registry")
    if not registry.has_reconstructed_priors():
        raise VerdictRefused("the trial registry lacks the reconstructed prior trials (md_eval.prior_trials): the trial count would be understated")
    if set(results) != set(ROLES):
        raise VerdictRefused(f"results are exactly {sorted(ROLES)}")
    ids = {role: _bind(role, results[role], sha, holdout) for role in ROLES}
    for role, i in ids.items():
        if not registry.contains(i.get("registry_head_sha256")):
            raise VerdictRefused(f"{role}: the trial registry presented does not contain the registry head the run recorded (LOW-R1)")
    s, s2, c = results["strategy"], results["strategy_2x"], results["control"]
    st = {role: _settings(i) for role, i in ids.items()}
    if any(st[role] != st["strategy"] for role in ROLES):
        raise VerdictRefused(f"the three runs differ in {sorted({k for role in ROLES for k in st[role] if st[role][k] != st['strategy'][k]})}")
    off = _against_prereg(st["strategy"], prereg)
    if off:
        raise VerdictRefused(f"the runs' settings differ from the pre-registration: {sorted(off)}")
    if [r["session"] for r in s["rows"]] != [r["session"] for r in c["rows"]] or [r["session"] for r in s["rows"]] != [r["session"] for r in s2["rows"]]:
        raise VerdictRefused("the three runs cover different sessions")
    if ids["strategy"]["source"] != ids["strategy_2x"]["source"]:
        raise VerdictRefused("strategy_2x is not the same strategy")
    strat, ctrl = ids["strategy"]["source"], ids["control"]["source"]
    ps, pc = prereg["strategy"], prereg["control"]
    if strat.get("name") != ps["id"] or strat.get("version") != ps["version"] or strat.get("preregistration_hash") != ps["preregistration_hash"]:
        raise VerdictRefused("the strategy run is not the pre-registration's strategy (id, version and preregistration_hash are all required)")
    if ctrl.get("name") != pc["id"] or ctrl.get("version") != pc["version"] or ctrl.get("preregistration_hash") != ps["preregistration_hash"]:
        raise VerdictRefused("the control run is not the pre-registration's control (id, version and the frozen hash are all required; "
                             "beats_control means beating it)")
    for k in ("universe_id", "static_map_sha256"):                    # LOW-1: the control runs on the strategy's universe and classification
        if ctrl.get(k) != strat.get(k):
            raise VerdictRefused(f"the control's {k} ({ctrl.get(k)!r}) differs from the strategy's ({strat.get(k)!r})")
    seg_rng = prereg["segments"][ids["strategy"]["segment_label"]]
    if prereg["hypothesis_id"] not in registry.trial_count(ids["strategy"]["content_sha256"], seg_rng[0], seg_rng[1], data_exposure_unknown=True)["counted"]:
        raise VerdictRefused("the trial being judged is not counted in the registry's trial count (MED-2)")
    bench = ids["strategy"]["benchmark"]
    if bench["instrument_id"] != prereg["benchmark_instrument"]:
        raise VerdictRefused("the runs' benchmark is not the pre-registered benchmark_instrument")
    if bench["status"] != "AVAILABLE" or not isinstance(s["benchmark_returns"], list):
        raise VerdictRefused(f"the pre-registered benchmark's total-return series is UNAVAILABLE ({s['benchmark_returns']}): no verdict, and never a "
                             "fallback to raw prices")
    spy_returns = s["benchmark_returns"]
    segment, dataset = ids["strategy"]["segment_label"], ids["strategy"]["content_sha256"]
    op = None
    if segment == "TEST":
        op = holdout.test_open_entry(sha, dataset)
        if op is None or op["seq"] <= rec["seq"]:
            raise VerdictRefused("a TEST verdict needs TEST_OPENED for this pre-registration and dataset, recorded after PREREG_RECORDED")
        if holdout.test_result_entry(op["seq"]) is not None:
            raise VerdictRefused("this TEST_OPENED already has its TEST_RESULT: one look, one result (MED-3)")
    if ids["strategy"]["kind"] == "HISTORICAL" and ids["strategy"]["licence"] != "CONFIRMED":
        raise LicenceRefused("HISTORICAL evidence without personal_research_use = CONFIRMED")

    out = _decide(s, s2, c, ids, prereg, sha, rec, op, spy_returns, registry)
    if op is not None:
        out["test_result"] = holdout.record_test_result(sha, dataset, out["identities"], out["verdict"], op["seq"])["entry_sha256"]
    return out


def _decide(s, s2, c, ids, prereg, sha, rec, op, spy_returns, registry) -> dict:
    th, b = prereg["thresholds"], prereg["bootstrap"]
    segment, dataset = ids["strategy"]["segment_label"], ids["strategy"]["content_sha256"]
    flags = sorted(set(s["flags"]) | set(s2["flags"]) | set(c["flags"]))
    out = {"verdict": "NOT_ESTABLISHED", "reasons": [], "caps": [], "thresholds": dict(th), "prereg_sha256": sha, "prereg_seq": rec["seq"],
           "segment": segment, "identities": {"strategy": s["identity_sha256"], "strategy_2x": s2["identity_sha256"], "control": c["identity_sha256"]},
           "flags": flags, "holdout_flags": list((op or {}).get("payload", {}).get("flags", [])), "evidence": {}}
    em = BM.exposure_matched(s, spy_returns)
    diffs = BM.daily_differences(s, em["returns"])
    ev = out["evidence"]
    ev["n_sessions"] = len(diffs)
    missing = []
    if len(diffs) < 2:
        missing.append("daily_differences")
    else:
        q = (1.0 - b["confidence"]) * 100.0
        bs = IN.stationary_bootstrap(diffs, b["mean_block_sessions"], b["resamples"], b["seed"], percentiles=(q, 50.0, 100.0 - q))
        ev.update({"observed_mean_difference": bs["observed"], "bootstrap_lower": bs["percentiles"][str(q)], "p_value_one_sided": bs["p_value_one_sided"],
                   "bootstrap": {k: bs[k] for k in ("method", "mean_block", "resamples", "seed", "n")}})
    rs, rc = MT.daily_returns([r["equity"] for r in s["rows"]]), MT.daily_returns([r["equity"] for r in c["rows"]])
    ex = [a - x for a, x in zip(rs[1:], rc[1:])]
    ev["mean_excess_over_control"] = math.fsum(ex) / len(ex) if ex else None
    ev["beats_control"] = ev["mean_excess_over_control"] is not None and ev["mean_excess_over_control"] > 0
    ev["net_positive_at_2x"] = s2["rows"][-1]["equity"] > ids["strategy_2x"]["initial_cash"]      # hashed fields only (MED-3)
    if th["min_dsr"] is not None:
        seg = prereg["segments"][segment]
        k = registry.trial_count(dataset, seg[0], seg[1], data_exposure_unknown=True)["k"]
        basis = prereg["dsr_basis"]
        x = diffs if basis == "EXCESS_OVER_EXPOSURE_MATCHED_SPY" else [rs[t] - s["rows"][t]["cash_return"] for t in range(1, len(rs))]
        if len(x) < 3:
            missing.append("dsr")
        else:
            ev["dsr"] = dict(IN.dsr(x, k), basis=basis)
    if missing:
        out["reasons"].append({"code": "MISSING_INPUT", "fields": missing})
        return out
    caps = sorted(set(flags) & set(VERDICT_CAPS))
    if caps:
        out["caps"] = caps
        out["reasons"].append({"code": "CAPPED", "flags": caps})
        return out
    if not ev["observed_mean_difference"] > 0:
        out["reasons"].append({"code": "NO_POSITIVE_DIFFERENCE"})
        return out
    if ev["net_positive_at_2x"] is not True:
        out["reasons"].append({"code": "NOT_NET_POSITIVE_AT_2X"})
        return out
    if ev["n_sessions"] < th["n_min_sessions"]:
        out["verdict"] = "INCONCLUSIVE"
        out["reasons"].append({"code": "BELOW_N_MIN", "n_sessions": ev["n_sessions"], "n_min_sessions": th["n_min_sessions"]})
        return out
    checks = {"p_value": ev["p_value_one_sided"] <= th["alpha"], "bootstrap_lower_bound": ev["bootstrap_lower"] > 0, "beats_control": ev["beats_control"]}
    if th["min_dsr"] is not None:
        checks["dsr"] = ev["dsr"]["dsr"] is not None and ev["dsr"]["dsr"] >= th["min_dsr"]
    failed = sorted(k for k, ok in checks.items() if not ok)
    if failed:
        out["verdict"] = "INCONCLUSIVE"
        out["reasons"].append({"code": "CRITERIA_NOT_MET", "failed": failed})
    elif segment != "TEST":
        out["verdict"] = "INCONCLUSIVE"
        out["reasons"].append({"code": "ESTABLISHED_ONLY_ON_AN_OPENED_TEST_SEGMENT", "segment": segment})
    else:
        out["verdict"] = "ESTABLISHED"
        out["reasons"].append({"code": "ALL_PREREGISTERED_CRITERIA_MET"})
    return out
