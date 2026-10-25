# DESIGN 6.2 — Strategy Validation Harness

Stage 6.2 adds a research validation harness that tries to disprove a strategy before paper trading. Methodology, constants and limitations:
`STRATEGY_VALIDATION_HARNESS.md`. This document is the architecture and the boundaries.

* **Backtest results are hypothetical.**
* **PASS does not mean profitable in the future.**
* **PASS does not auto-promote a strategy.** It means *eligible for human review*; the lifecycle stays a human decision.

## Architecture

```
Strategy (registered; adapter = identity + execution assumptions + candidate generation)
   ↓
Candidate Generation              ONCE; Strategy Standard v1; missing data skipped + counted, never zero
   ↓
Immutable Candidate Set           content identity per candidate; candidate_set_hash
   ↓
RULES / GATED / MODEL             the SAME set; GATED may only veto, MODEL may only TAKE / STAND_ASIDE (NOT_EVALUATED by default)
   ↓                              mismatch → VALIDATION INVALID, nothing simulated
Simulation                        next-bar-open fills, STOP_FIRST, gross / slippage / fees / net, risk-based sizing
   ↓
TRAIN / OOS                       chronological split (70 / 30 default), OOS reported on its own
   ↓
Metrics                           per arm × segment; mean_R_t_stat as evidence
   ↓
Mechanical Audit                  independent re-derivation from stored records + raw bars: N / N trades validated
   ↓
Verdict                           INSUFFICIENT DATA · FAIL · MIXED · PASS · VALIDATION INVALID
   ↓
Human Lifecycle Review            outside the harness; the registry lifecycle is never written by it
```

Product boundary:

```
STRATEGY LAB
   ├── Registry
   ├── Validation   (Backtest · OOS · Controls · Audit)    ← Stage 6.2, display + CLI only
   └── Lifecycle → human review → PAPER_CANDIDATE
NOT: backtest passes → automatically trades
```

## Modules (`strategy_validation/`)

| Module | Role |
|---|---|
| `__init__.py` | constants: verdicts, arms, sample-size / diagnostics policy, friction and sizing defaults, the disclaimer, `VERSION = validation_v1` |
| `models.py` | `Bar`, `RawCandidate`, `StrategyCandidate` (frozen), `SkippedCandidate`, `ArmDecision`, `SimulatedTrade`, `ArmResult`, `CausalityReport`, `AuditResult`, `ValidationRun` (pydantic, extra=forbid) |
| `dataset.py` | the bar dataset (OHLC validated, content hash `dataset_id`), CSV / JSON load, the chronological split |
| `adapters.py` | the adapter contract; `DownsideRecoveryAdapter` declares no honest replay (`NOT_RUN`, reason); placeholders → `NOT_RUN` |
| `candidates.py` | normalization: identity, Strategy Standard v1, skips with reasons |
| `causality.py` | static scan + runtime checks; `MANUAL_CAUSALITY_REVIEW_REQUIRED` always |
| `arms.py` | RULES / GATED / MODEL and the same-candidate-set check |
| `simulator.py` | one candidate → one hypothetical trade |
| `metrics.py` | per arm × segment metrics, t-stat, drawdown |
| `diagnostics.py` | train vs OOS, period concentration, ambiguous-bar dependence, friction weight, parameter sensitivity |
| `audit.py` | the independent mechanical audit (imports nothing from the package) |
| `verdict.py` | the deterministic verdict and decision line |
| `engine.py` | one run, end to end |
| `store.py` | append-only persistence; SYNTHETIC runs test-only |
| `service.py` | the registry / Strategy Lab read views |
| `replay.py` | the replay-validation foundation (research vs observed candidate states) |
| `cli.py`, `__main__.py` | `python -m strategy_validation validate | runs` |

Outside the package: `database/strategy_validation_migrations.py` (three additive append-only tables), `api/routes/strategy_validation.py`
(four GET routes, included by `api/routes/strategy_registry.py` under its prefix; `api/server.py` unchanged), `strategy_registry/service.py`
(validation fields on the rows, the `validation` block of the detail), `frontend/product_ui.js` / `.css` (the Validation column and block).

## Boundaries (enforced by tests)

* **No broker write**: no module of the harness, its route or its migration imports execution, paper, portfolio, proposals, safety,
  trader-view, Alpaca, Robinhood or network code, or calls an order / Paper Auto function; a CLI run with broker credentials in the environment
  and the network disabled completes with none of those modules loaded (`tests/test_strategy_validation_62.py` B01 / B02).
* **No lifecycle mutation**: the registry entries are identical before and after a stored PASS; the harness source writes no status.
* **GET only**: the routes are `router.get` only, take no body, and no path names a backtest launch, an order or a trade; a POST is 405.
* **No fake results**: a strategy without an honest replay is `NOT_RUN` (the engine raises instead of simulating); a run without trades reports
  `None`, never `0`, for every ratio; SYNTHETIC fixture runs never reach a normal process.
* **Frozen controls, execution, risk and safety untouched**: the Stage 6.1 frozen-controls hash is unchanged, and the freeze test pins every
  file against main except the listed Stage 6.2 files and exact edits.

## Data model (persisted)

`strategy_validation_runs (run_id, strategy_id, strategy_version, dataset_id, dataset_kind, verdict, validation_version, candidate_count,
created_at, run_json)` - the reproducible record: configuration snapshot (adapter description and parameters, split, arms, gates, model, sizing,
dataset source), friction model, execution assumptions, integrity (same candidate set, problems, set hash), causality, per-arm decisions /
metrics / diagnostics / verdicts, diagnostics, audit, verdict, git commit. `strategy_validation_candidates (run_id, candidate_id, signal_date,
symbol, candidate_json)` and `strategy_validation_trades (run_id, trade_id, arm, candidate_id, segment, trade_json)`. Rows are never updated or
deleted.

## Adversarial review (Stage 6.2, before the PR)

Ten areas were reviewed (look-ahead / causality, candidate-set equality, metric correctness, friction / execution assumptions, OOS contamination,
fake or unsupported results, mechanical-audit independence, lifecycle auto-promotion, the broker-write boundary, tests / CI). Findings and outcome:

| # | Severity | Area | Finding | Outcome |
|---|---|---|---|---|
| 1 | HIGH | audit / metrics | The audit did not check trades against the arm decisions: a vetoed candidate that still traded, or an accepted candidate whose (losing) trade disappeared, passed "N / N validated". | Fixed: `NOT_ACCEPTED` and `MISSING_TRADE` checks (every TAKE has a trade or a recorded skip). Tests U03. |
| 2 | MEDIUM | look-ahead | Runtime checks only saw look-ahead in dated features; a selection on future bars was invisible. | Fixed: point-in-time truncation-invariance check (`RUNTIME_TRUNCATION_INVARIANCE`, CRITICAL). Test L01b with a peeking fixture. |
| 3 | MEDIUM | friction / execution | A next-bar open above the target became an immediate TARGET exit at the target, below the fill (a fabricated loss labelled as a target). | Fixed: `ENTRY_GAP_THROUGH_TARGET` skip; the audit checks the target is above the entry. Test M02b. |
| 4 | MEDIUM | OOS contamination | "A change after OOS is a new strategy_version" was documented, not enforced. | Fixed: the store refuses another adapter configuration under a validated version. Test R03b. |
| 5 | MEDIUM | audit | The exit was checked for consistency with its bar, not for being the FIRST qualifying bar; sizing, segment labels, gross and R arithmetic were not re-derived. | Fixed: `EXIT_FIRST_TOUCH`, `SIZE`, `SEGMENT`, `GROSS`, `R_MULTIPLE` checks. Test U02 / U03. |
| 6 | LOW | API | `/validation` named the declared registry status `lifecycle_status`. | Fixed: `declared_status`. |
| 7 | LOW | OOS | TRAIN trades can exit inside the OOS window (no purge / embargo). | Deferred, reported: `diagnostics.boundary` counts them; nothing is fitted by the harness. |
| 8 | LOW | OOS | Sensitivity neighbours run on TRAIN + OOS. | Deferred: a diagnostic label only, never a selection (documented). |
| 9 | LOW | fake results | A JSON dataset's kind is self-declared. | Deferred: the content hash and source are recorded; no registered strategy can run today (all NOT_RUN). |
| 10 | LOW | arms | A failing gate aborts the run while an invalid model answer marks MODEL INVALID. | Deferred: both fail loudly, neither silently. |

No CRITICAL finding. Lifecycle auto-promotion and the broker-write boundary were verified, not changed: nothing in the package writes a status,
and a CLI run with broker credentials in the environment and the network disabled loads no execution / broker module (tests B01 / B02, R02).

## Tests

`tests/test_strategy_validation_62.py` (CI group `research`): candidates, arms, split / OOS, simulation and metrics, diagnostics, audit,
causality, verdicts, registry / store / routes, CLI, replay, determinism, the broker boundary, the display, the CI manifest, the freeze and the
documents. `tests/frontend/strategy_validation.test.cjs`: the Validation column and block. Browser flows `strategy_validation`,
`strategy_validation_results` (test-only registry entries and synthetic runs injected in the harness process; never in a preview).
Fixtures (`tests/strategy_validation_fixtures.py`, SYNTHETIC, never registered): GOOD (PASS), OVERFIT (FAIL), TOO_SMALL (INSUFFICIENT DATA),
FRICTION_KILLED (FAIL), LOOKAHEAD (VALIDATION INVALID), CANDIDATE_MISMATCH (VALIDATION INVALID), AMBIGUOUS (MIXED).
