# Stage 6.1.2 — QuantConnect strategy adapter architecture (DESIGN, not built)

QuantConnect is a strategy SOURCE in the registry's data model (`source = QUANTCONNECT`), not a broker and not an integration. This document fixes the
architecture a future stage must follow. Stage 6.1.2 builds none of it: LEAN is not wired, no QuantConnect strategy is registered, no network call,
no credential, no execution path.

## 1. Architecture

```
QuantConnect / LEAN
   ↓   strategy research / backtest (QuantConnect's own tooling; results stay research evidence)
normalized strategy signal            Strategy Standard v1 (STRATEGY_STANDARD_V1.md): symbol, direction, score, conditions, risk class, generated_at
   ↓
Stock Agent Strategy Adapter          validates the signal (strategy_registry.standard.validate_signal), maps it to the registry entry, attaches it as evidence
   ↓
Opportunity Ranking                   Stage 6.2+: ranks READY opportunities across strategies (none exists today: one strategy, alphabetical scan)
   ↓
Portfolio / Risk / Safety             the Stage 5.9 review proposal, the Stage 5.9.1 safety gate (kill switch, daily loss, pause), the position caps
   ↓
Alpaca Paper                          the Stage 6.0 executor: exactly-once paper orders behind every entry control; PAPER host only
```

## 2. Boundaries (never bypassed)

A QuantConnect strategy, like any strategy, MUST NOT bypass: Opportunity Ranking, Portfolio allocation, Risk, Safety, the Proposal, the Execution
controls. Its signals are evidence. The adapter never calls a broker, never places, previews or cancels an order, never changes the execution mode and
never writes safety state. A signal that carries an order, a quantity, a notional, a broker, an account or an execution instruction is rejected by the
standard (`EXECUTION_FIELD_FORBIDDEN`).

## 3. The adapter's responsibilities (future)

* Accept Strategy Standard v1 objects (file drop, LEAN research export or a local push) and validate every one against the registry: unknown, not
  implemented or inactive strategies are rejected (`STRATEGY_UNKNOWN`, `STRATEGY_NOT_IMPLEMENTED`, `STRATEGY_INACTIVE`); a source mismatch is rejected.
* Normalise the score as a RANK (0-100) unless the strategy documents a calibration; never present a score as a probability otherwise.
* Attach the signal to the symbol's evidence for Opportunity Ranking; the proposal, the gate and the executor keep deciding entries and exits with their
  unchanged rules. The adapter adds no new order path.
* Record provenance (strategy_id, source, version, generated_at) so the WHY panel can name the strategy behind a READY and the Strategy Lab can show
  the signal history; nothing is fabricated when no signal exists.

## 4. What exists today

* The registry accepts `source = QUANTCONNECT` entries (tests: `tests/test_product_flow_612.py` R-tests).
* `GET /api/strategy-registry` reports `quantconnect: { integrated: false, registered: 0, pipeline: [...] }` and the Strategy Lab panel shows
  `QUANTCONNECT · 0 … not integrated (adapter architecture only)`.
* `strategy_registry.standard` validates the normalized object the adapter will consume.

## 5. Not built in this stage

LEAN execution or research integration, QuantConnect credentials, any QuantConnect strategy, Opportunity Ranking, signal ingestion, signal storage.
