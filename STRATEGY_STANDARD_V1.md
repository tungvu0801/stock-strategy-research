# Strategy Standard v1

The normalized object every strategy - internal or QuantConnect - must eventually produce. A signal is EVIDENCE: it names a symbol, a direction,
conditions and a score. It never names an order, a quantity, a broker or an account, and no strategy places a broker order. Validation:
`strategy_registry.standard.validate_signal(obj, registry)`; schema: `GET /api/strategy-registry/standard`. Version string: `strategy_standard_v1`.

## 1. Fields

| Field | Required | Type / enum | Rule |
|---|---|---|---|
| `strategy_id` | yes | UPPER_SNAKE_CASE, 3-64 chars | must exist in the registry, be implemented and in an active lifecycle status (PAPER_CANDIDATE, PAPER_ACTIVE, APPROVED) |
| `source` | yes | INTERNAL · QUANTCONNECT | must match the registry entry's source |
| `asset_class` | yes | US_EQUITY | |
| `direction` | yes | LONG · SHORT | the strategy's `direction_capability` bounds it (the product is long-only today) |
| `symbol` | yes | upper-case ticker | |
| `score` | yes | number 0-100 | a deterministic RANK value by default; **not a probability** |
| `time_horizon` | yes | INTRADAY · SWING · POSITION | |
| `entry_condition` | yes | non-empty text | the rule, in words |
| `invalidation_condition` | yes | non-empty text | |
| `exit_condition` | yes | non-empty text | |
| `risk_class` | yes | LOW · MODERATE · HIGH · UNCLASSIFIED | |
| `generated_at` | yes | ISO 8601 | |
| `version` | yes | non-empty text | the standard the object follows (`strategy_standard_v1`) |
| `score_kind` | optional | RANK (default) · CALIBRATED_PROBABILITY | CALIBRATED_PROBABILITY needs a documented `calibration`; a RANK carries none |
| `calibration` | optional | text | how the probability was calibrated |
| `evidence` | optional | object | deterministic facts; no execution field anywhere inside |

Forbidden keys at any depth (rejected as `EXECUTION_FIELD_FORBIDDEN`): `order`, `orders`, `order_type`, `qty`, `quantity`, `shares`, `notional`, `broker`,
`broker_order_id`, `client_order_id`, `submit`, `submitted`, `execute`, `execution`, `side`, `limit_price`, `stop_price`, `time_in_force`, `account`,
`account_id`, `buy`, `sell`, `place_order`, `cancel`, `leverage`. Unknown top-level keys are rejected (`UNKNOWN_FIELD`): optional facts belong under
`evidence`.

## 2. Errors

`MISSING_FIELD` · `UNKNOWN_FIELD` · `EXECUTION_FIELD_FORBIDDEN` · `INVALID_VALUE` (enum, range, timestamp, empty condition) · `SCORE_NOT_CALIBRATED` ·
`STRATEGY_UNKNOWN` · `STRATEGY_NOT_IMPLEMENTED` (a placeholder never emits a signal) · `STRATEGY_INACTIVE` · `SOURCE_MISMATCH` · `DIRECTION_NOT_SUPPORTED`
(the direction is outside the registry entry's `direction_capability`).

## 3. Example (illustration of the shape, not a real signal)

```json
{"strategy_id": "DOWNSIDE_DISLOCATION_RECOVERY", "source": "INTERNAL", "asset_class": "US_EQUITY", "direction": "LONG", "symbol": "XYZ", "score": 72,
 "time_horizon": "SWING", "entry_condition": "setup READY: stretch <= -40, confidence >= 40, demand >= 60, fresh evidence",
 "invalidation_condition": "stretch > -20 or demand < 35 on two evaluations or an evidence contradiction", "exit_condition": "invalidation, evidence, setup, demand, time (5 sessions), normalisation",
 "risk_class": "MODERATE", "generated_at": "2026-10-25T14:00:00Z", "version": "strategy_standard_v1"}
```

## 4. Where it sits

```
Strategy (evidence / signal) -> Opportunity / Proposal -> Portfolio -> Risk / Safety -> Execution -> Broker (Alpaca PAPER)
```

The standard is enforced before anything downstream; the proposal, the gate and the executor keep their own, unchanged controls. Tests:
`tests/test_product_flow_612.py` (R-tests).
