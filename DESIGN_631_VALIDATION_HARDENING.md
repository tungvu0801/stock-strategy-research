# DESIGN 6.3.1 — Validation harness hardening (integrity)

Stage 6.3.1 fixes the two harness-level gaps Stage 6.3 found (DESIGN_63 §9, §10), and nothing else: no strategy is added or changed,
Downside Dislocation Recovery stays **NOT_RUN**, and no execution, risk, safety or Paper Auto behaviour changes. Methodology version:
`validation_v2`.

```
candidate generation (once)
   ↓
chronological TRAIN / OOS split
   ↓
boundary-safe simulation      TRAIN candidates: PURGE when the holding window crosses the TRAIN end; otherwise simulated on bars <= TRAIN end
   ↓
metrics                       per arm × TRAIN / OOS / ALL, plus simulated_trade_count and boundary_purged_count
   ↓
causality / parity checks     automated checks (the TRAIN end is always a point-in-time cut) + the DECLARED manual review + declared replay parity
   ↓
mechanical audit              independent; now also: no TRAIN trade uses an OOS bar, the purge is exact both ways
   ↓
accounting invariant          accepted = simulated + boundary_purged + skipped (per arm and segment) - else VALIDATION INVALID
   ↓
integrity gate                PersistenceEligibility: ELIGIBLE / BLOCKED with every check and reason
   ↓
eligible persistence          the store re-evaluates the gate itself; a BLOCKED run is never written
   ↓
human review
```

## 1. TRAIN / OOS boundary: PURGE

* **Candidate classification** is by signal date: signal ≤ TRAIN end → TRAIN, else OOS (unchanged).
* **Outcome eligibility** additionally respects the boundary: a TRAIN candidate whose **holding window** - the next `max_hold_bars`
  sessions of its symbol after the signal, i.e. every bar its trade could use - contains a bar after the TRAIN end is **purged**
  (`TRAIN_OOS_BOUNDARY_PURGE`), *whatever its outcome*. The rule reads bar dates only, never a price, so it cannot select on a result: a
  near-boundary winner is purged exactly like a near-boundary loser (the Stage 6.3 policy, DESIGN_63 §9).
* Every other TRAIN candidate is simulated on the dataset **cut at the TRAIN end** (`Dataset.until`), so its outcome physically cannot
  read an OOS price. OOS candidates are simulated on the full dataset.
* A purged candidate **stays a TRAIN candidate** (it counts in `candidate_count` / `accepted_count` of TRAIN) and contributes to **no**
  TRAIN or OOS metric (P/L, R, t-stat, Sharpe, drawdown …). It is never relabelled OOS, never truncated at an arbitrary price, never dropped
  silently: it is a `simulation_skips` entry with its reason and detail, `boundary.purged[arm]` lists the candidate ids,
  `boundary.train_boundary_purged_count` and the per-segment `boundary_purged_count` count it.
* The parameter-sensitivity neighbours use the same policy.
* Example (the brief's): TRAIN ends 2025-12-31, signal 2025-12-29, entry 2025-12-30, exit would be 2026-01-03 → candidate TRAIN, outcome
  PURGED, reason `TRAIN_OOS_BOUNDARY_PURGE`.

**OOS independence** (`tests/test_validation_integrity_631.py::test_B03`): the same TRAIN bars with wildly different OOS prices give
identical TRAIN metrics (every key, both evaluated arms), while the OOS metrics differ and - the negative control - the boundary candidate
simulated on the full data does change between the two datasets.

## 2. Accounting invariant (`strategy_validation/integrity.py` `reconcile`)

Per evaluated arm, as **disjoint candidate-id sets**: `accepted = simulated + boundary_purged + skipped` (skips by reason:
`NO_ENTRY_BAR`, `ENTRY_GAP_THROUGH_STOP`, `ENTRY_GAP_THROUGH_TARGET`), `candidates = accepted + stand_aside`, the same split by TRAIN / OOS,
no candidate in two terminal categories, no outcome without an accepted candidate, no OOS candidate purged. Before the arms:
`generated = candidates + skipped_before_arms` (incl. `missing_data_skipped`). Any mismatch: accounting **FAIL** → the verdict is
**VALIDATION INVALID** (`ACCOUNTING_MISMATCH`) and the gate blocks. The run records `accounting` (status, per-arm counts, per-segment counts).

## 3. Persistence integrity gate (`PersistenceEligibility`)

`evaluate(record)` reads the run **record** (plain data, exactly as stored) and returns `status` ELIGIBLE / BLOCKED, `integrity_status`
PASS / FAIL, every check (PASS / FAIL / NOT_APPLICABLE + detail) and the failing reasons. Checks, in order:

| Check | Blocks when |
|---|---|
| STRUCTURE | a required field is missing (ids, versions, dates, verdict, RULES metrics, config snapshot, audit, candidate set) |
| STRATEGY_VERSION | missing, or inconsistent across the run, the adapter description and the candidates |
| PROVENANCE | a declared field is missing (§4), `bar_frequency` ≠ 1Day, `retrieved_at` not an ISO date, a derived field disagrees with the run, a conditional field the adapter requires is missing, or the adapter never declared which conditional fields apply |
| CANDIDATE_SET | the arms did not receive the generated candidate set, or its hash does not re-compute |
| ARM_CONTRACT | an arm is INVALID (e.g. a model that tried to change a trade) |
| CAUSALITY_AUTOMATED | any CRITICAL automated finding (incl. the point-in-time truncation check, which always cuts at the TRAIN end) |
| CAUSALITY_MANUAL_REVIEW | the adapter did not declare its manual review status, or declared COMPLETED without a reference. PENDING is allowed and stored as PENDING - `manual_review_required` stays true and PENDING is never shown as reviewed |
| REPLAY_PARITY | undeclared; a live implementation exists (declared by the adapter, or an implemented registry entry with a setup type) but parity is not EXACT_REQUIRED; EXACT_REQUIRED but not ESTABLISHED; ESTABLISHED without a reference to the parity tests. NOT_APPLICABLE (research-only strategy, no separate live implementation) passes |
| MECHANICAL_AUDIT | not PASS, any violation, or incomplete (trades or candidates not all checked) |
| ACCOUNTING | the invariant (§2) fails |
| BOUNDARY | the policy is not PURGE, or a TRAIN trade exits after the TRAIN end |
| PERFORMANCE_VERDICT | the verdict is VALIDATION INVALID (the engine found an integrity failure) |

**Integrity is not performance.** Integrity PASS + verdict FAIL or INSUFFICIENT_DATA is a valid, persistable research result. Integrity
FAIL is never persisted, however profitable it looks (the LOOKAHEAD fixture: mean R > 0.5, BLOCKED).

**Store enforcement**: `StrategyValidationStore.insert` re-evaluates the gate from the record (a run's own `eligibility` field is never
trusted) and raises `PersistenceBlocked` with the reasons; nothing is written. The stored record carries the gate result.

## 4. Provenance required for persistence

| Field | Source |
|---|---|
| `source`, `retrieved_at` (ISO), `bar_frequency` (1Day), `adjustment`, `missing_data_policy` | declared by the dataset (a JSON dataset's `provenance` object, or `--provenance FILE` for a CSV) |
| `dataset_id` (content hash), `start`, `end`, `symbols`, `sessions`, `kind`, `source_file` | derived from the bars, never taken from a declaration |
| `strategy_id`, `strategy_version`, `validation_version`, `config_snapshot` | the run |
| `corporate_actions`, `universe`, `point_in_time` | required only when the adapter declares them (`provenance_requires`); the decision is explicit - `None` (undeclared) blocks, `()` records "not required by the adapter" |

A run with incomplete provenance executes normally (ephemeral research / debug) but cannot be persisted.

## 5. Replay parity declarations (adapter)

`live_implementation` (module path or None), `replay_parity_requirement` (EXACT_REQUIRED | NOT_APPLICABLE), `replay_parity_status`
(ESTABLISHED | NOT_ESTABLISHED), `replay_parity_reference` (the parity tests). A strategy with no separate live implementation is not forced
into a live-parity check. Downside Dislocation Recovery declares `setups/engine.py`, EXACT_REQUIRED, NOT_ESTABLISHED (DESIGN_63) - and the
engine refuses it before any gate is reached (no honest historical replay).

## 6. CLI

`--persist` is still required to store anything. With `--persist`: run → gate → ELIGIBLE: stored (exit 0); BLOCKED (or refused by the store):
**not stored, exit 3**, the failing checks printed - never silently downgraded to an ephemeral run. Without `--persist` the run is ephemeral
and its integrity result is printed (exit 0). Exit codes: 0 completed, 1 input error, 2 NOT_RUN, 3 not stored (blocked / refused).
`--provenance FILE` supplies declared provenance for a dataset file without it.

## 7. API / UI / storage

* No new route and nothing that triggers a validation; the GET views add `integrity_status`, `persistence_eligibility`,
  `boundary_purged_count`, `accounting_status` (registry rows and the detail) and the gate checks, accounting, boundary, provenance and replay
  parity (the run detail as `integrity_gate`, `accounting`, `boundary`, `provenance`, `replay_parity`, and `/audit`; the run detail's `integrity`
  stays the Stage 6.2 candidate-set integrity).
* Strategy Lab: an **Integrity** line (status · persistence · accounting · boundary purges), a "Purged at the TRAIN / OOS boundary" metric row,
  the declared manual review status. A Stage 6.2 run shows "Integrity: not recorded"; a strategy without a run shows nothing new.
* Storage: no schema change. The new fields live in the append-only `run_json`; Stage 6.2 (`validation_v1`) records stay readable unchanged and
  are never back-filled; the same-version configuration check compares the strategy-defining adapter fields only.

## 8. Adversarial review (Stage 6.3.1)

| # | Severity | Area | Finding | Outcome |
|---|---|---|---|---|
| 1 | HIGH | OOS leakage | The first implementation purged by *outcome* (unresolved by the TRAIN end), which keeps fast near-boundary exits and purges slow ones - a selection on the result - and differs from the documented policy. | Fixed: purge by the holding window (dates only, DESIGN_63 §9); test B06 shows a near-boundary winner purged too. |
| 2 | HIGH | hidden dropped trades | A purge could be used to hide a TRAIN trade. | Fixed: the audit re-derives the purge both ways (`UNJUSTIFIED_PURGE`, `BOUNDARY_NOT_PURGED`) and checks no TRAIN trade exits after the TRAIN end (`TRAIN_USES_OOS`). |
| 2b | HIGH | backward compatibility / UI | The first read view put the gate result under the run detail's existing `integrity` key (the Stage 6.2 candidate-set integrity), so a valid run would have been displayed as "the arms did not receive the same candidate set". Found by the browser flow. | Fixed: the gate block is `integrity_gate`; tests assert both blocks are present. |
| 3 | MEDIUM | OOS leakage | The point-in-time causality check sampled its cuts; a TRAIN candidate generated from OOS data could slip between samples. | Fixed: the TRAIN end is always a cut (test B08). |
| 4 | MEDIUM | replay parity | An adapter could declare parity ESTABLISHED with no evidence. | Fixed: ESTABLISHED needs a parity-test reference. |
| 5 | MEDIUM | provenance | `retrieved_at` accepted any text. | Fixed: must be an ISO date / datetime. |
| 6 | MEDIUM | accounting | The Strategy Lab metric whitelist dropped `boundary_purged_count` / `simulated_trade_count`. | Fixed (service). |
| 7 | MEDIUM | audit | For a mismatched candidate set (never simulated by design) the completeness check reported every accepted candidate as missing. | Fixed: completeness applies to simulated runs only. |
| 8 | LOW | provenance | Declared provenance (source, adjustment, missing-data policy) is self-declared, not verified against the data source; the missing-data policy is not enforced by the harness. | Deferred, documented. |
| 9 | LOW | audit gate | The store cannot re-run the audit (the bars are not stored); a hand-built `ValidationRun` object with a forged PASS audit would pass the gate. Every CLI / engine path produces the audit itself. | Deferred, documented. |
| 10 | LOW | causality | WARN findings (undated features, future-index patterns) do not block persistence; only CRITICAL does (as specified). | Deferred, documented. |
| 11 | LOW | OOS | Sensitivity neighbours are labelled on TRAIN + OOS expectancy (a diagnostic label; no parameter is selected). | Deferred, documented. |
| 12 | LOW | backward compatibility | A Stage 6.2 record would still display its verdict (with "Integrity: not recorded"); no real Stage 6.2 run exists. | Accepted. |

No CRITICAL finding. Broker isolation: the new module imports nothing outside the package, pydantic and the standard library; the Stage 6.2
boundary tests scan every `strategy_validation/*.py` (incl. `integrity.py`); no route writes.
