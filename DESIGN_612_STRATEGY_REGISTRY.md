# Stage 6.1.2 — Strategy Registry architecture (DESIGN)

The Strategy Lab foundation: a read-only registry of strategies, their lifecycle status and their documented / live evidence. A strategy PRODUCES
evidence and signals; it never executes. Execution stays `Strategy -> Opportunity / Proposal -> Portfolio -> Risk / Safety -> Execution -> Broker`
(Alpaca PAPER). Nothing in this package or its route mutates, orders, activates, tunes or calls out.

## 1. Data model (`strategy_registry/models.py`, pydantic, extra = forbid)

`StrategyEntry`: `strategy_id` (UPPER_SNAKE_CASE), `display_name`, `source` (INTERNAL | QUANTCONNECT), `asset_class` (US_EQUITY), `direction_capability`
(LONG | SHORT | LONG_SHORT), `time_horizon` (INTRADAY | SWING | POSITION), `status` (lifecycle, below), `description`, `version`, `created_at`,
`updated_at`, plus `implemented` (the only claim of existing logic), `setup_type` (implemented strategies only) and `placeholder_note`. A strategy that
is not implemented can only be RESEARCH (or REJECTED) and cannot name a setup type.

Lifecycle: `RESEARCH -> BACKTEST_CANDIDATE -> REVIEW -> PAPER_CANDIDATE -> PAPER_ACTIVE -> APPROVED | REJECTED`. Active statuses (the only ones whose
strategy may appear as a live setup / signal source): PAPER_CANDIDATE, PAPER_ACTIVE, APPROVED. APPROVED / REJECTED are never derived - they need a
recorded human decision, and no decision record exists yet.

`Section` (a detail block): `state` DOCUMENTED (from code / design documents) · LIVE (from persisted records) · NOT_EVALUATED (nothing exists:
"Not yet evaluated") · UNAVAILABLE (the process cannot read it right now). A NOT_EVALUATED or UNAVAILABLE section carries no items or facts; a
DOCUMENTED or LIVE one needs at least one item. `sources` name the files, documents or routes every item comes from.

## 2. Registry entries (`strategy_registry/registry.py`, code-defined, no table)

| strategy_id | source | implemented | declared status | notes |
|---|---|---|---|---|
| DOWNSIDE_DISLOCATION_RECOVERY | INTERNAL | yes | PAPER_CANDIDATE | the Stage 5.6-6.1 path; setup_type = setups.SETUP_TYPE; created 2026-10-21 (Stage 5.8 merged), updated 2026-10-24 (Stage 6.1 merged) |
| VALUE_QUALITY, MOMENTUM, MEAN_REVERSION, BREAKOUT | INTERNAL | no | RESEARCH | placeholders: no rules, no data, no backtest, no result, no signal |
| (none) | QUANTCONNECT | — | — | the source is supported by the model; nothing is registered, nothing is wired |

## 3. Lifecycle derivation (`strategy_registry/service.py`, deterministic, documented)

* A placeholder keeps its declared status.
* The implemented strategy is declared PAPER_CANDIDATE (the Stage 6.1 forward paper validation exists for it) and reads PAPER_ACTIVE only while a
  persisted fact says so: at least one recorded validation session (`GET /api/forward-validation` -> `sessions.completed`), or Paper Auto currently ON
  (`GET /api/execution/status` -> `mode == PAPER_AUTO`). The basis is returned with the status.
* The facts are read through the existing Trader View stream services (`execution.mode()`, `forward_validation.overview()`); a missing service is
  reported in `facts.errors`, never invented.

## 4. Detail sections (`strategy_registry/downside_recovery.py`)

Overview · Hypothesis · Rules · Data requirements · Entry logic · Invalidation · Exit logic · Holding horizon · Backtest · Out-of-sample · Risk notes ·
Paper results · Decision log. For DOWNSIDE_DISLOCATION_RECOVERY every threshold comes from `forward_validation.frozen_controls()` (the frozen Stage 6.1
contract) and the module constants of `setups` / `proposals` / `execution`; the wording quotes `DESIGN_56` … `DESIGN_61`. Backtest and Out-of-sample are
NOT_EVALUATED (no backtest of this setup exists; the Stage 4.8-5.2 modules replay the portfolio rotation model, a different family; the PPE / Demand
replays are component diagnostics). Decision log is NOT_EVALUATED (no decision record exists). Paper results are LIVE from the forward paper validation
summary (sessions, closed trades, strategy status, attention) or UNAVAILABLE when the summary cannot be read. Placeholders: the overview says what they
are; every other section is NOT_EVALUATED.

## 5. API (`api/routes/strategy_registry.py`, GET only)

* `GET /api/strategy-registry` - `strategies[]` (entry fields + `declared_status`, derived `status`, `status_label`, `lifecycle_basis`, `source_label`,
  `active`), `sources[]` (INTERNAL / QUANTCONNECT with counts), `lifecycle[]`, `active_statuses`, `pipeline`, `quantconnect` (integrated false,
  registered 0, the adapter pipeline), `facts`, `counts`, `disclaimer`.
* `GET /api/strategy-registry/standard` - the Strategy Standard v1 schema (`STRATEGY_STANDARD_V1.md`).
* `GET /api/strategy-registry/{strategy_id}` - `entry`, `lifecycle`, `sections[]`, `links`, `disclaimer`; 404 for an unknown id, 400 for a malformed one.

No POST / PUT / PATCH / DELETE exists; no route path contains trade, paper, order, broker or execute.

## 6. UI (MORE › Research › Strategy Lab, `frontend/product_ui.js`)

The registry table (name, source, lifecycle badge - placeholders read `placeholder · not active` -, horizon, direction, version, updated), the source
chips (`INTERNAL · 5`, `QUANTCONNECT · 0 … not integrated`), the pipeline line, the legacy research workspaces under a fold, and the detail view with the
thirteen sections as folds (Documented / Live / Not yet evaluated / Unavailable badges, Technical details with the raw facts, the sources). The only
actions are navigation (back to the registry, Open TRADE, the Forward Validation page). The TRADE strategy display reads the same registry: only an
implemented, active strategy can be "Best setup / Best strategy"; placeholders never appear.

## 7. Adding a strategy later

1. Register it (`registry.py`) as RESEARCH with `implemented = False` until its rules exist.
2. Implement it to emit Strategy Standard v1 signals (`strategy_registry/standard.py` validates them; a placeholder or an inactive strategy is rejected).
3. Route the signals through the Stock Agent Strategy Adapter into Opportunity Ranking (Stage 6.2+), then the unchanged proposal / gate / executor path.
4. Move it along the lifecycle only on recorded facts (backtest, review, paper validation) and record every approval / rejection decision; the registry
   never derives APPROVED / REJECTED.
