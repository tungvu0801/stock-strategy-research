"""md_eval/registry.py - MD-4: the trial registry and the holdout ledger, both APPEND-ONLY, HASH-CHAINED JSON-lines files at caller-given paths
(the evidence root outside the repository; a temporary folder in tests). Each line is canonical JSON
  {"seq", "kind", "created_at", "payload", "prev_sha256", "entry_sha256"}
where entry_sha256 = sha256 of the canonical line without entry_sha256, prev_sha256 is the previous line's entry_sha256 (64 zeros first).
The whole chain is verified before every read and every append: an EDITED or REORDERED line, or one removed from the middle, refuses the file.
The chain alone does NOT detect a truncated tail or a deleted file (what remains is still a valid chain). That is detected by comparison:
every engine run records the chain HEAD it saw (head(), the last entry_sha256) in its result identity, and every TEST_RESULT records the head
before it; the verdict refuses a result whose recorded head is no longer in the chain (contains()).

Trial registry kinds:  TRIAL_REGISTERED (a pre-registration's sha256, its hypothesis, family, strategy, dataset sha or TO_BE_ACCEPTED, segments),
                       RECONSTRUCTED_PRIOR (md_eval.prior_trials, once; every count a lower bound).
Holdout ledger kinds:  PREREG_RECORDED (a pre-registration's sha256, recorded BEFORE any look),
                       VALIDATION_RUN / TEST_RUN (written by the engine after each VALIDATION / TEST run: the run's identity_sha256, result_sha256 and
                       benchmark series sha256, its source name, cost multiplier and role GATING / RANDOM_CONTROL, so the verdict can bind its evidence
                       to a recorded run; VALIDATION_RUN carries an attempt count; at most ONE TEST_RUN per (pre-registration, dataset, source, cost
                       level), MED-3),
                       TEST_OPENED (the family, the dataset content_sha256, the TEST calendar range, the pre-registration sha256 and its PREREG_RECORDED
                       seq - order by the chain, never by a clock). The holdout is keyed on the CALENDAR range: a second TEST_OPENED whose range
                       overlaps an opened one is refused WHATEVER the dataset (one look spends that calendar period); a range the dataset's
                       data_exposure shows as already seen is flagged TEST_RANGE_PREVIOUSLY_EXPOSED (DATA_EXPOSURE_UNKNOWN when not recorded).
                       Fail closed across pre-registrations (MED-3, enforced by engine.segment_guard): a HISTORICAL or segment-labelled run whose
                       range overlaps the TEST range of ANY OTHER registered pre-registration that has no TEST_OPENED yet is refused
                       (OVERLAPS_UNOPENED_TEST_OF_OTHER_PREREG), so no run reads another hypothesis's holdout without a record,
                       TEST_RESULT (written by the verdict: the three result identities, the verdict, the TEST_OPENED seq and the head before it;
                       exactly ONE per TEST_OPENED, MED-3)."""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, List, Optional

from md_eval import prior_trials as PT
from md_eval import prereg as PR

GENESIS = "0" * 64


class ChainError(RuntimeError):
    pass


class HoldoutRefused(RuntimeError):
    pass


def _canonical(obj) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"))


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _overlap(a, b) -> bool:
    return a[0] <= b[1] and b[0] <= a[1]


RANDOM_CONTROL_PREFIX = "RANDOM_ENTRY_CONTROL"


def run_role(source_name: str) -> str:
    """RANDOM_CONTROL for the report-only random-entry control (LOW-N1); GATING otherwise."""
    return "RANDOM_CONTROL" if str(source_name).startswith(RANDOM_CONTROL_PREFIX) else "GATING"


class HashChainLog:
    KINDS: tuple = ()

    def __init__(self, path, clock: Callable[[], str] = _utc_now):
        self.path, self.clock = Path(path), clock

    def entries(self) -> List[dict]:
        if not self.path.exists():
            return []
        out, prev = [], GENESIS
        for n, line in enumerate(self.path.read_text(encoding="utf-8").split("\n")):
            if line == "":
                continue
            e = json.loads(line)
            body = {k: v for k, v in e.items() if k != "entry_sha256"}
            if _canonical(e) != line or e.get("seq") != len(out) or e.get("prev_sha256") != prev or e.get("kind") not in self.KINDS:
                raise ChainError(f"{self.path.name} line {n + 1}: not the next canonical entry of the chain")
            if hashlib.sha256(_canonical(body).encode("utf-8")).hexdigest() != e.get("entry_sha256"):
                raise ChainError(f"{self.path.name} line {n + 1}: entry_sha256 mismatch (edited)")
            out.append(e)
            prev = e["entry_sha256"]
        return out

    def head(self) -> Optional[str]:
        """The chain head (the last entry_sha256), or None for an empty chain: recorded by runs so a later truncation is detectable."""
        es = self.entries()
        return es[-1]["entry_sha256"] if es else None

    def contains(self, entry_sha256: Optional[str]) -> bool:
        return entry_sha256 is not None and any(e["entry_sha256"] == entry_sha256 for e in self.entries())

    def append(self, kind: str, payload: dict) -> dict:
        if kind not in self.KINDS:
            raise ValueError(kind)
        es = self.entries()                                          # verifies the whole chain first
        body = {"seq": len(es), "kind": kind, "created_at": self.clock(), "payload": payload,
                "prev_sha256": es[-1]["entry_sha256"] if es else GENESIS}
        e = dict(body, entry_sha256=hashlib.sha256(_canonical(body).encode("utf-8")).hexdigest())
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.path, "a", encoding="utf-8", newline="\n") as f:
            f.write(_canonical(e) + "\n")
        return e


class TrialRegistry(HashChainLog):
    KINDS = ("TRIAL_REGISTERED", "RECONSTRUCTED_PRIOR")

    def register(self, prereg: dict) -> dict:
        sha = PR.validate(prereg)
        if self.has_trial(sha):
            raise ChainError("this pre-registration is already registered")
        return self.append("TRIAL_REGISTERED", {"prereg_sha256": sha, "hypothesis_id": prereg["hypothesis_id"], "family": list(prereg["family"]),
                                                "strategy": dict(prereg["strategy"]), "control": dict(prereg["control"]),
                                                "benchmark_instrument": prereg["benchmark_instrument"],
                                                "dataset_sha256": prereg["dataset_sha256"], **{k: prereg[k] for k in PR.SETTINGS_KEYS}})

    def reconstruct_priors(self) -> List[dict]:
        if any(e["kind"] == "RECONSTRUCTED_PRIOR" for e in self.entries()):
            raise ChainError("the prior trials are already reconstructed in this registry")
        return [self.append("RECONSTRUCTED_PRIOR", dict(p, count_is_lower_bound=True)) for p in PT.PRIOR_TRIALS]

    def has_reconstructed_priors(self) -> bool:
        """LOW-R1: every md_eval.prior_trials entry is present as a RECONSTRUCTED_PRIOR (the conservative k depends on them)."""
        have = {e["payload"].get("prior_id") for e in self.entries() if e["kind"] == "RECONSTRUCTED_PRIOR"}
        return {p["prior_id"] for p in PT.PRIOR_TRIALS} <= have

    def trial_entry(self, prereg_sha256: Optional[str]) -> Optional[dict]:
        for e in self.entries():
            if e["kind"] == "TRIAL_REGISTERED" and e["payload"]["prereg_sha256"] == prereg_sha256:
                return e
        return None

    def has_trial(self, prereg_sha256: Optional[str]) -> bool:
        return bool(prereg_sha256) and self.trial_entry(prereg_sha256) is not None

    def trial_count(self, dataset_sha256: str, start: str, end: str, data_exposure_unknown: bool) -> dict:
        """k for the deflated Sharpe: the registered trials on this dataset or pre-registered as TO_BE_ACCEPTED (MED-2: they may run on any
        dataset, so they count; k only goes up) + the reconstructed priors whose range overlaps [start, end], or all of them when the data
        exposure is UNKNOWN or the prior's range is UNKNOWN. Always a lower bound."""
        k, parts = 0, []
        for e in self.entries():
            p = e["payload"]
            if e["kind"] == "TRIAL_REGISTERED" and p["dataset_sha256"] in (dataset_sha256, "TO_BE_ACCEPTED"):   # MED-2: a TO_BE_ACCEPTED trial
                k += 1                                                                                       # may run on any dataset: counted
                parts.append(p["hypothesis_id"])
            elif e["kind"] == "RECONSTRUCTED_PRIOR":
                rng = p["range"]
                overlap = isinstance(rng, list) and rng[0] <= end and start <= rng[1]
                if data_exposure_unknown or not isinstance(rng, list) or overlap:
                    k += int(p["variants_lower_bound"])
                    parts.append(p["prior_id"])
        return {"k": max(k, 1), "counted": parts, "count_is_lower_bound": True}


class HoldoutLedger(HashChainLog):
    KINDS = ("PREREG_RECORDED", "VALIDATION_RUN", "TEST_RUN", "TEST_OPENED", "TEST_RESULT")

    def record_prereg(self, prereg: dict) -> dict:
        sha = PR.validate(prereg)
        if self.prereg_entry(sha) is not None:
            raise HoldoutRefused("this pre-registration is already recorded")
        return self.append("PREREG_RECORDED", {"prereg_sha256": sha, "hypothesis_id": prereg["hypothesis_id"], "family": list(prereg["family"])})

    def prereg_entry(self, prereg_sha256: Optional[str]) -> Optional[dict]:
        for e in self.entries():
            if e["kind"] == "PREREG_RECORDED" and e["payload"]["prereg_sha256"] == prereg_sha256:
                return e
        return None

    def record_validation_run(self, prereg_sha256: str, dataset_sha256: str, identity_sha256: str, result_sha256: str,
                              benchmark_series_sha256: Optional[str] = None, source_name: str = "", cost_multiplier: float = 1.0) -> dict:
        if self.prereg_entry(prereg_sha256) is None:
            raise HoldoutRefused("no pre-registration recorded before this VALIDATION run")
        n = sum(1 for e in self.entries() if e["kind"] == "VALIDATION_RUN" and e["payload"]["prereg_sha256"] == prereg_sha256
                and e["payload"]["dataset_sha256"] == dataset_sha256)
        return self.append("VALIDATION_RUN", {"prereg_sha256": prereg_sha256, "dataset_sha256": dataset_sha256, "attempt": n + 1,
                                              "identity_sha256": identity_sha256, "result_sha256": result_sha256,
                                              "benchmark_series_sha256": benchmark_series_sha256, "source_name": source_name,
                                              "cost_multiplier": float(cost_multiplier), "role": run_role(source_name)})

    def test_run_exists(self, prereg_sha256: str, dataset_sha256: str, source_name: str, cost_multiplier: float) -> bool:
        """MED-3: one TEST attempt per (pre-registration, dataset, source, cost level)."""
        return any(e["kind"] == "TEST_RUN" and e["payload"]["prereg_sha256"] == prereg_sha256 and e["payload"]["dataset_sha256"] == dataset_sha256
                   and e["payload"]["source_name"] == source_name and e["payload"]["cost_multiplier"] == float(cost_multiplier) for e in self.entries())

    def record_test_run(self, prereg_sha256: str, dataset_sha256: str, identity_sha256: str, result_sha256: str,
                        benchmark_series_sha256: Optional[str] = None, source_name: str = "", cost_multiplier: float = 1.0) -> dict:
        if self.test_open_entry(prereg_sha256, dataset_sha256) is None:
            raise HoldoutRefused("no TEST_OPENED entry for this TEST run")
        if self.test_run_exists(prereg_sha256, dataset_sha256, source_name, cost_multiplier):
            raise HoldoutRefused("this source at this cost level already ran on the TEST segment: one TEST attempt")
        return self.append("TEST_RUN", {"prereg_sha256": prereg_sha256, "dataset_sha256": dataset_sha256, "identity_sha256": identity_sha256,
                                        "result_sha256": result_sha256, "benchmark_series_sha256": benchmark_series_sha256,
                                        "source_name": source_name, "cost_multiplier": float(cost_multiplier), "role": run_role(source_name)})

    def run_entry(self, kind: str, prereg_sha256: str, dataset_sha256: str, identity_sha256: str) -> Optional[dict]:
        for e in self.entries():
            p = e["payload"]
            if e["kind"] == kind and p["prereg_sha256"] == prereg_sha256 and p["dataset_sha256"] == dataset_sha256 and p["identity_sha256"] == identity_sha256:
                return e
        return None

    def open_test(self, prereg: dict, dataset_sha256: str, data_exposure=None) -> dict:
        sha = PR.validate(prereg)
        rec = self.prereg_entry(sha)
        if rec is None:
            raise HoldoutRefused("a TEST look requires the pre-registration's sha256 recorded BEFORE the look")
        rng = list(prereg["segments"]["TEST"])
        for e in self.entries():
            if e["kind"] == "TEST_OPENED" and _overlap(e["payload"]["test_range"], rng):
                a, b = e["payload"]["test_range"]
                raise HoldoutRefused(f"the TEST calendar range {a}..{b} was already opened (seq {e['seq']}, dataset {e['payload']['dataset_sha256'][:12]}): "
                                     "one look spends that period, whatever the dataset")
        flags = []
        if data_exposure is None or isinstance(data_exposure, dict) or not isinstance(data_exposure, (list, tuple)):
            flags.append("DATA_EXPOSURE_UNKNOWN")
        elif any(_overlap(x["range"], rng) for x in data_exposure if isinstance(x, dict) and isinstance(x.get("range"), (list, tuple))):
            flags.append("TEST_RANGE_PREVIOUSLY_EXPOSED")
        return self.append("TEST_OPENED", {"prereg_sha256": sha, "prereg_seq": rec["seq"], "family": list(prereg["family"]),
                                           "dataset_sha256": dataset_sha256, "test_range": rng, "flags": flags})

    def test_opened_for(self, prereg_sha256: str) -> bool:
        """MED-3: whether this pre-registration's TEST range was opened (on any dataset; the calendar range is what is spent)."""
        return any(e["kind"] == "TEST_OPENED" and e["payload"]["prereg_sha256"] == prereg_sha256 for e in self.entries())

    def test_open_entry(self, prereg_sha256: Optional[str], dataset_sha256: str) -> Optional[dict]:
        for e in self.entries():
            p = e["payload"]
            if e["kind"] == "TEST_OPENED" and p["prereg_sha256"] == prereg_sha256 and p["dataset_sha256"] == dataset_sha256:
                return e
        return None

    def test_result_entry(self, test_opened_seq: int) -> Optional[dict]:
        for e in self.entries():
            if e["kind"] == "TEST_RESULT" and e["payload"]["test_opened_seq"] == test_opened_seq:
                return e
        return None

    def record_test_result(self, prereg_sha256: str, dataset_sha256: str, result_identities: dict, verdict: str, test_opened_seq: int) -> dict:
        """MED-3: exactly one TEST_RESULT per TEST_OPENED (a second is refused); it records the head before it (LOW-1)."""
        op = self.test_open_entry(prereg_sha256, dataset_sha256)
        if op is None or op["seq"] != test_opened_seq:
            raise HoldoutRefused("no TEST_OPENED entry for this result")
        if self.test_result_entry(test_opened_seq) is not None:
            raise HoldoutRefused("this TEST_OPENED already has its TEST_RESULT: one look, one result")
        return self.append("TEST_RESULT", {"prereg_sha256": prereg_sha256, "dataset_sha256": dataset_sha256, "test_opened_seq": test_opened_seq,
                                           "result_identities": dict(result_identities), "verdict": verdict, "holdout_head_sha256": self.head()})
