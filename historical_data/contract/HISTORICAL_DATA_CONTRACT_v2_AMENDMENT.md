# Historical data contract v2 = v1 revision 2 + this amendment (normative), 2026-11-16

**Base:** `HISTORICAL_DATA_CONTRACT_v1.md`, revision 2 final, sha256 `453676fd44554ac9f66ce2d168318893ea18d93916ea2eef3ffa0f8c4de40fd7`, unchanged.
- **v2 is the base plus the items below.** Where an item conflicts with the base, the item wins.
- `contract_version = "historical_data_contract_v2"`.
- `contract_sha256` = the sha256 of **this file**, which pins the base by its sha above.
- **Why:** the two Developers' read-only plans found genuine gaps before any code was written. Every item is additive or a clarification, and no principle (P1–P7) is weakened.
- **Status: APPROVED (2026-11-16).** The delta review returned APPROVE WITH CHANGES, and the six changes were applied verbatim. The sha256 is recorded in `CONTRACT_SHA256.txt`.

## A1. Session enumeration (MD-B Q1)
§4 adds two methods, both derived only from the §2 authoritative calendar:
- `sessions(start_session, end_session) -> list[session]` returns calendar sessions in ascending order, inclusive;
- `next_session(session) -> session` returns the next calendar session. It raises `CALENDAR_NOT_COVERED` past 2026-12-31.

## A2. Execution channel signature (MD-A Q-1)
§4's `execution_open` becomes `execution_open(instrument_id, session, *, decision_session)`.
- **It raises** unless `session == next_session(decision_session)`.
- **It returns** `raw_open`, or `NO_BAR` when the bar is missing or was removed as invalid. `NO_BAR` gives `NO_FILL_GAP`.
- **It exists only on the provider,** never on the `PointInTimeView`. The view holds no reference to the provider.

## A3. All orders fill at the next open; no intraday stops in v2 (MD-B Q2)
- P2 applies to **exits too:** stop, target and time exits are decided after a close and filled at the next session's open.
- **Any run of a strategy whose frozen rules specify intraday stop or close-time exits** (for example V1/C1) carries the flag `EXITS_NEXT_OPEN`.
- Its results are **not comparable** with figures produced under the frozen intraday execution.
- **Price levels carried across sessions** (stops, targets) are divided by the split `ratio` at the split booking, or are held in split-adjusted terms under P1 exception (i).

## A4. Instrument classification fields (MD-B Q3)
§3.1 adds two optional fields, each a value or UNKNOWN:
- `sector` (string);
- `issuer_id`, e.g. a SEC CIK as `SEC:<cik10>`.

A consumer that needs either one while it is UNKNOWN may supply a **static map as a run parameter**. The map's sha256 goes into the result identity, and the run is flagged `STATIC_CLASSIFICATION_MAP`, which marks today's classification applied backwards (not point-in-time).

## A5. Valuation on a gap session (MD-B Q4)
- A held position on a session with no bar is **marked for valuation only** at its last raw close at or before that session, flagged `STALE_MARK`.
- This is a valuation rule, **not** a data fill: §3.2's "a gap is never forward-filled" still holds for every provider output and every decision input.
- **Sizing** from equity that includes a `STALE_MARK` is permitted and flagged.
- **A held instrument with no bar from some session to `end_session` and no `DELISTING` event** is flagged `POSSIBLE_DELISTING`, so it is not stale-marked silently.

## A6. Rebased factors in the view, and test (i) tolerance (MD-A D-1, R2; MD-B Q5)
- `PointInTimeView` exposes `split_factor` and `total_return_factor` **rebased**: each instrument's factors are divided by its factor on the **last bar visible at t**, so that bar's factor is exactly 1.0. Absolute factor levels never reach decision logic.
- **Test (i):** decisions must be identical **exactly** for scale constants that are powers of two (e.g. 2.0, 0.5). For other constants (e.g. 0.37, 1e6 + 1), decision-relevant quantities may differ by at most **1e-12 relative**, and decisions must be identical.
- Consumers may canonicalise window ratios to 12 significant digits before threshold comparisons. If they do, the rule is recorded in the result identity.
- **If the factor on the last visible bar is UNAVAILABLE,** the view's factors for that instrument are UNAVAILABLE.
- **The test (i) fixture** has no decision-relevant quantity within 1e-9 (relative) of a threshold.

## A7. CORPORATE_ACTION_UNACCOUNTED caps the verdict (MD-B Q7)
§7's verdict caps add: **any `CORPORATE_ACTION_UNACCOUNTED` flag caps the verdict at NOT_ESTABLISHED.** It is conservative: incomplete corporate actions can bias returns in either direction.

## A8. Symbol at a session when the history is UNKNOWN (MD-B Q6)
With `symbol_history_status = UNKNOWN`, the instrument's single `symbol_history` entry applies to **every** session in the dataset. The consumer matches universes by that symbol, and the run is flagged `SYMBOL_HISTORY_UNKNOWN`.

**P1 gains two declared exceptions:**
- **(iii) `STATIC_CLASSIFICATION_MAP` (A4).** Flagged, and it **caps the verdict at NOT_ESTABLISHED**.
- **(iv) `SYMBOL_HISTORY_UNKNOWN` (A8).** Flagged.

Both are today's facts applied backwards.

## A9. Expected-events table vs "matching event" (MD-A Q-4, Q-2, D-2)
- **A coded expected-events table is reference data, not a provider event.** It is never emitted by `events()`, never booked, and never changes `events_completeness`.
- **For WARN classification only,** a table entry with status **VERIFIED** whose session and type match a `DAY_JUMP` reclassifies that jump from WARN `DAY_JUMP_UNCLASSIFIED` to the record's `classified_jumps` list (with the table entry ID).
  - Status INFERRED, UNVERIFIED or UNRESOLVED never classifies; it is listed only. (This is more conservative than MD-A's D-2, which also counted INFERRED.)
- **"Spin-off indicated"** means a provider `SPIN_OFF` event, or a VERIFIED table spin-off. **"Not adjusted"** means `total_return_factor` is UNAVAILABLE, or does not change by more than 1e-5 at `effective_session`.
- **Ticker reuse (SNDK):** the contract's rules stand. A BLOCK (`TICKER_REUSE_COLLISION`) is raised only when two instruments share a symbol at one session. A symbol-only gap over 20 sessions is WARN `POSSIBLE_TICKER_REUSE`, which a table rule may annotate but never upgrade or suppress. The gate's "BLOCK on reused tickers" is read through these rules.

## A10. Licence file reader (MD-A Q-3)
- **v2 defines no licence-file loader.** MD-A ships the licence schema and the checks only. Adapters always emit `UNKNOWN`. MD-B's licence gate reads the manifest's licence block.
- The owner-authored licence file, its format and its loader are specified at the data-source gate. Until then, no HISTORICAL dataset can be `CONFIRMED`, so no real-data computation of returns or signals is possible.

## A11. The view's manifest
The manifest available through `PointInTimeView` is an **allow-list**. It carries only:
- `contract_version`, `contract_sha256`, `kind`;
- `provider`, `adapter`, `adapter_version`;
- `start_session`;
- `price_views_available`, `volume_feed`, `price_source`, `membership_basis`, `events_completeness`;
- `licence`.

Every other field, including `end_session`, `instrument_count`, `content_sha256`, `dataset_id`, `adjustment_as_of`, `retrieved_at`, `source_files` and `data_exposure`, is withheld, because each can reveal the data's future extent. Consumers read the full manifest only before a run (version and licence checks) and for reporting.

## A12. Versioning procedure
- **The v2 sha256 is computed after this file's final edit** and is recorded outside the file, in `CONTRACT_SHA256.txt`.
- **Before use, MD-A's and MD-B's tests verify** that the base file's sha256 equals `453676fd44554ac9f66ce2d168318893ea18d93916ea2eef3ffa0f8c4de40fd7` and that this file's sha256 equals the recorded v2 sha.
- **Base wording:** where the base says `historical_data_contract_v1` (§6.1), v2 manifests carry `historical_data_contract_v2` (item precedence). A cleaner base text is deferred to any future v3.
