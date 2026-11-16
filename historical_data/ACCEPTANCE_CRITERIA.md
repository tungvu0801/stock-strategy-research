# Historical dataset acceptance criteria (MD-A, contract v2)

These are the rules that `historical_data/acceptance.py` (`decide`) applies to a dataset's acceptance record (`historical_data/validate.py`). They exist so that MD-7's acceptance step is mechanical. Every rule cites the contract; none is new policy.

**Contract:** `HISTORICAL_DATA_CONTRACT_v1.md` revision 2 final (sha256 `453676fd…0fd7`) plus `HISTORICAL_DATA_CONTRACT_v2_AMENDMENT.md` (sha256 `7850580b…6ed8`). Every manifest and acceptance record carries `contract_version = historical_data_contract_v2` and both shas.

## 1. The acceptance record (section 5)
- **BLOCK** (the dataset is unusable):
  - `DUPLICATE_KEY`: two bars with one (instrument, session) key, or two events with one `event_id`;
  - `NON_CALENDAR_SESSION` and `CALENDAR_NOT_COVERED`, for the sessions of bars, events, membership changes and scheduled events;
  - `KNOWN_AT_MISMATCH`;
  - `FACTOR_NOT_FINITE_OR_NONPOSITIVE`;
  - `FACTOR_CHANGE_WITHOUT_EVENT` (only when `events_completeness = COMPLETE`);
  - `TICKER_REUSE_COLLISION`;
  - `SPLIT_SERIES_MISSING`, whether the series is absent wholly or only at a session;
  - `MANIFEST_HASH_MISMATCH`;
  - `CALENDAR_CROSSCHECK_DISAGREEMENT`;
  - `TZDATA_UNAVAILABLE`.
- **WARN:**
  - `DAY_JUMP_UNCLASSIFIED`;
  - `GAP_2_OR_MORE_SESSIONS`;
  - `POSSIBLE_TICKER_REUSE`;
  - `SPIN_OFF_NOT_ADJUSTED`;
  - `VOLUME_FEED_NOT_CONSOLIDATED` and `PRICE_SOURCE_NOT_CONSOLIDATED`;
  - `INFERRED_SPLIT`;
  - `SYMBOL_DROPPED_BY_ADAPTER`: a symbol in the source that yields no instrument (only error envelopes, or no valid bar). This is an MD-A addition to the section 5 list (QA L-5): a lost symbol is never silent. The record's `adapter_notes` carries the counts and the symbol lists.
- **The verdict:** `BLOCK` if any BLOCK reason exists, else `WARN` if any WARN reason exists, else `CLEAN`. Every reason is listed with its instrument, its session and a detail.
- **A jump over 25 %** is classified (it moves to `classified_jumps`, not a WARN) only by:
  - a provider event of a price-moving type between the two bars; or
  - a **VERIFIED** expected-events table entry with the same symbol, session and type (A9).

  INFERRED, UNVERIFIED, OBSERVED, RULE and UNRESOLVED entries never classify a jump. They are listed in `expected_events_listed`.
- **Ticker reuse:** the table's ticker-reuse rule (SNDK) may annotate a `POSSIBLE_TICKER_REUSE`. It never upgrades it to BLOCK and never suppresses it (A9).

## 2. What a dataset may be used for (`usable_for`)
| Condition | `usable_for` | Reason code | Rule |
|---|---|---|---|
| The record's verdict is BLOCK | `NONE` | `DATASET_BLOCKED` | §5 |
| The record names another dataset (dataset_id or content_sha256) | `NONE` | `RECORD_IS_NOT_THIS_DATASET` | §5, §6.3 |
| `personal_research_use = NOT_PERMITTED` | `NONE` (no use except deletion) | `LICENCE_NOT_PERMITTED` | §6.2 |
| `kind = HISTORICAL` and `personal_research_use ≠ CONFIRMED` | `INTEGRITY_ONLY` | `LICENCE_NOT_CONFIRMED` | §6.2, A10 |
| No raw open/high/low (`RAW_OHL` not in `price_views_available`) | `INTEGRITY_ONLY` | `RAW_OHL_UNAVAILABLE` | P3, §4.1 |
| Otherwise | `EVALUATION` | — | |

**Under `INTEGRITY_ONLY`:**
- only schema, integrity and validation checks run;
- no returns, signals, ledger runs or performance statistics are computed;
- the provider's `as_of` and `execution_open` refuse (`LicenceRefused` / `IntegrityOnly`).

Adapters always emit `UNKNOWN`, and a synthetic fixture may declare `kind = HISTORICAL` only with that `UNKNOWN` licence. v2 defines no licence-file loader (A10), so **no dataset can be CONFIRMED in v2**, and no HISTORICAL dataset is usable for evaluation.

## 3. Caps and flags that travel with every result
- **Verdict caps.** Each of these caps an edge or alpha claim at NOT_ESTABLISHED:
  - `FIXED_CURRENT_UNIVERSE` (`SURVIVORSHIP_BIAS_PRESENT`, §3.4);
  - `events_completeness ≠ COMPLETE` (`CORPORATE_ACTION_UNACCOUNTED`, P3a, A7);
  - no split-adjusted view (signals only in a run flagged `SPLIT_UNADJUSTED`, P3).
- **Flags:**
  - `EARNINGS_DATA_UNAVAILABLE` (§3.5: D.3-bound strategies produce no candidates, and that is reported);
  - `SYMBOL_HISTORY_UNKNOWN` (A8);
  - `STATIC_CLASSIFICATION_MAP_NEEDED` when sector or issuer is UNKNOWN (A4: a run that supplies a map is flagged and capped).

## 4. The v1 adapters as they stand
| Adapter | Kind | Licence | Expected `usable_for` | Caps and flags |
|---|---|---|---|---|
| `SyntheticProvider` | SYNTHETIC | UNKNOWN (exempt) | EVALUATION, for fixtures only | as declared by the fixture |
| `Ms1EvidenceProvider` (MS-1 IEX raw archive) | HISTORICAL | UNKNOWN | INTEGRITY_ONLY | FIXED_CURRENT_UNIVERSE, CORPORATE_ACTION_UNACCOUNTED, SPLIT_UNADJUSTED; IEX single venue (WARN) |
| `SipDailyV1Provider` (no real dataset exists) | as the bundle says | UNKNOWN | INTEGRITY_ONLY when HISTORICAL | FIXED_CURRENT_UNIVERSE, CORPORATE_ACTION_UNACCOUNTED |
