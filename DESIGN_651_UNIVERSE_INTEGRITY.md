# Stage 6.5.1 — Survivorship / point-in-time universe integrity gate

> This stage is about DATA INTEGRITY, not strategy performance. No strategy rule, threshold, friction assumption, control definition or stored
> result changes. Nothing places an order, enables Paper Auto, or changes execution, risk or safety.

## 1. The problem: survivorship bias

A historical replay must not know which companies survive into the future. A 2021 rebalance that ranks "today's 81 large caps" already knows
that each of them stayed listed until 2026, avoided bankruptcy, was not acquired and grew (or stayed) large enough to be on today's list. The
stocks that failed, shrank, were bought or were delisted after 2021 are simply absent. That leaks the future into:

* the strategy portfolio (it can only pick survivors) and the eligible-universe control (it holds only survivors);
* sector composition (today's winners' sectors are over-represented);
* the ranking itself - most acutely for momentum: a stock is on today's list partly BECAUSE its run continued (CLS; SNDK, listed in 2025);
* apparent robustness, drawdowns and every selection-edge statistic.

**A fair control does not remove it.** The validation_v3 selection control (an equal-weight portfolio of the same eligible universe) isolates
the ranking's edge WITHIN the chosen universe: both sides share the universe, so the bias of the universe's average cancels in the excess. But
the bias is not only in the average: when the survivorship correlates with what the strategy ranks on (price momentum, quality, low leverage...),
the survivors favour exactly the stocks the strategy selects, and the excess over the control inherits that. A same-universe control can never
prove that the universe itself was historically unbiased: a fair control does not remove survivorship bias, so a PASS on control parity (8 / 8)
is never evidence about the universe.

## 2. The model

Every `CROSS_SECTIONAL_PORTFOLIO` adapter DECLARES its universe (`universe_declaration`); nothing is inferred and nothing defaults to trusted.

| Method | Meaning |
|---|---|
| `POINT_IN_TIME` | historical membership is known at each rebalance from information available then (a security master / membership history with additions, removals, delisted, acquired and bankrupt securities) |
| `HISTORICAL_SNAPSHOT` | dated historical constituent snapshots (e.g. monthly lists), possibly with gaps |
| `FIXED_CURRENT_UNIVERSE` | today's list projected backwards (current survivors) |
| `UNKNOWN` | the method is not known |

The declaration carries `universe_method`, `universe_source`, `membership_as_of_semantics`, `includes_delisted`, `includes_acquired`,
`includes_bankrupt`, `historical_additions_supported`, `historical_removals_supported`, `survivorship_bias`, `coverage_start`, `coverage_end`,
`notes`. An unknown value is `None` - never filled in.

## 3. Integrity status (policy universe_integrity_v1, fixed before it was applied to any stored result)

| Method | Integrity | Rule |
|---|---|---|
| `POINT_IN_TIME` | **TRUSTED** | only when the membership history is SUPPLIED (not just claimed), it covers the dataset (coverage_start <= first session, coverage_end >= last session), and the declaration says delisted, acquired and bankrupt securities and historical additions and removals are all represented |
| `POINT_IN_TIME` | LIMITED | membership history supplied, but a coverage or completeness condition above fails |
| `POINT_IN_TIME` | UNTRUSTED | declared but no membership history supplied (a claim that cannot be checked) |
| `HISTORICAL_SNAPSHOT` | **TRUSTED** | every rebalance has a snapshot dated on / before it and at most 31 days older, and delisted securities, additions and removals are all represented |
| `HISTORICAL_SNAPSHOT` | LIMITED | otherwise |
| `FIXED_CURRENT_UNIVERSE` | **UNTRUSTED** | always (current survivors projected backwards) |
| `UNKNOWN` | **UNTRUSTED** | always |
| (event-driven strategy) | NOT_APPLICABLE | no universe ranking |
| (no declaration) | — | a cross-sectional run without a declaration is BLOCKED from persistence (integrity check `UNIVERSE_INTEGRITY`) and its selection edge reads INVALID - never a silent default |

Tightened during the adversarial review (section 11; every change only lowers a status, and no stored result uses either point-in-time method):
a supplied membership must be the one the strategy ranked, in both directions, or the universe is UNTRUSTED; an unchecked snapshot claim is
UNTRUSTED; TRUSTED also needs `survivorship_bias: ABSENT`, no member excluded for missing price data, and at least one recorded removal
when delisted / acquired / bankrupt securities are claimed (otherwise LIMITED). Dates are `YYYY-MM-DD` only.

## 4. The selection-edge cap

The validation_v3 statistics and thresholds are unchanged and still computed: the **raw selection edge** (NOT_ESTABLISHED / WEAK /
ESTABLISHED / INSUFFICIENT_DATA / INVALID) is kept and shown. The **final selection edge** is the raw edge lowered to the cap of the universe
integrity, along NOT_ESTABLISHED < WEAK < ESTABLISHED:

| Universe integrity | Highest final selection edge |
|---|---|
| TRUSTED | no cap (the v3 policy) |
| LIMITED | WEAK |
| UNTRUSTED (FIXED_CURRENT_UNIVERSE, UNKNOWN, an unchecked POINT_IN_TIME claim) | NOT_ESTABLISHED |

INSUFFICIENT_DATA and INVALID are never raised or lowered. A capped edge carries the reason `UNIVERSE_INTEGRITY_CAP`. The overall verdict follows
the FINAL edge through the unchanged v3 mapping (so a cross-sectional strategy on an untrusted universe can never reach PASS, and therefore can
never become a paper candidate on that validation alone). Absolute performance, the control comparison and the raw edge stay visible.

**Run integrity vs selection-edge trust.** A technically correct replay on a biased universe is still a valid replay: the run integrity (the
existing checks) is unaffected, and a declared FIXED_CURRENT_UNIVERSE run persists. The new integrity check `UNIVERSE_INTEGRITY` requires a valid
declaration, a correct integrity classification and a correctly applied cap - it fails only on a missing / invalid declaration or a record whose
recorded integrity or final edge does not follow this policy.

## 5. The point-in-time contract

At rebalance T, membership must be reconstructable from information available as of T:

* a security is a member at T iff `effective_from <= T` and (`effective_to` is None or `T < effective_to`) - a future addition never appears
  before its effective date; a security that is later delisted, acquired or bankrupt is still a member before its removal date and disappears
  only from it;
* removals carry a reason (DELISTED, ACQUIRED, BANKRUPT, INDEX_REMOVAL, OTHER); a security keeps one identity across ticker changes (a dated
  symbol history), so a renamed security is neither dropped nor duplicated;
* intervals of one security never overlap; the history itself must never be built from today's survivors (e.g. today's index constituents
  projected backwards).

`strategy_validation/universe.py` implements this contract (`PointInTimeMembership`) for future datasets; no current dataset supplies one.

## 6. Existing stored results

They are never rewritten. A separate, append-only **universe-integrity assessment** (`strategy_validation_universe_assessments`) references the
run (and the methodology evaluation, if the raw edge came from one), records the strategy's declaration and the policy version, and derives the
final edge and overall verdict from the RECORDED raw edge. The run row and its evaluation stay byte-identical.

The store re-checks every assessment before appending it: the run (and evaluation) exist and match; the raw edge and the absolute verdict are the
recorded ones; the declaration is the registered strategy's own; the integrity, the final edge and the overall verdict follow this policy; the
timestamp is not in the future. Update and delete are refused by triggers, like every other validation table.

```
python -m strategy_validation assess-universe --run RUN_ID [--db PATH] [--persist] [--output FILE]
```

prints `universe METHOD -> STATUS; raw selection edge X -> final Y; overall Z (absolute performance P)` and, with `--persist`, appends the
assessment. The Strategy Lab reads the newest assessment over the methodology evaluation over the run.

## 7. Data readiness

What a point-in-time universe needs, and what this project has today:

| Requirement | Why | Today |
|---|---|---|
| Historical listing and delisting dates | a security is eligible only between them | no |
| A security master with a permanent identifier | one identity across ticker changes, mergers and re-listings (a ticker is not an identity) | partial: SEC CIK for filers, no master |
| Merger and acquisition events with effective dates | the target leaves the universe on the effective date, with its final consideration | no |
| Ticker-change history | a renamed security must neither drop out nor appear twice | no |
| Bankruptcy and delisting outcomes | a failed security's last return (often a large loss) must count, not vanish | no |
| Historical market capitalisation (shares outstanding as of T x price at T) | a size rule ("top N by market value") must use the size known at T | partial: SEC `dei` shares outstanding with filing dates; prices only for today's list |
| Index membership history | "member of index X" must mean member as of T | no |

**Do not use today's index or exchange list backwards.** Today's S&P 500, Nasdaq-100 or "today's large caps" projected over 2020-2026 is exactly a
FIXED_CURRENT_UNIVERSE, whatever its provenance; it is never a point-in-time universe.

**Delisting returns.** A point-in-time membership alone is not enough: the price series of a removed member must end at its removal with the
realised final value (the cash paid in an acquisition, the last traded price or the recovery in a bankruptcy). Dropping its bars silently
recreates the bias inside a point-in-time universe. No current dataset carries these, and the engine does not yet model them; a future
point-in-time dataset must supply them before its results can be read as TRUSTED in practice.

## 8. What SEC and Alpaca can and cannot give

**SEC EDGAR** (the source of the Value + Quality fundamentals):

* can: a stable company identifier (CIK) across ticker and name changes; filing histories of companies that later stopped filing; filing dates
  (so as-of knowledge is reconstructable); shares outstanding as reported, with dates; event filings that SIGNAL removals (for example
  completed acquisitions, bankruptcies, deregistration and exchange delisting notices) - as documents to parse, not as a clean event table;
* cannot: prices; an authoritative list of which securities were listed on a date; index membership; a complete, structured delisting or
  merger table. The ticker-to-CIK mapping file describes TODAY's tickers, so a universe built from it is a fixed current universe.

**Alpaca** (the price bars):

* can: daily bars for the symbols this project requests, and the current asset list with each asset's current status;
* cannot (as far as this project has used and verified it): historical membership or listing dates; a cross-vendor permanent security
  identifier (bars are keyed by ticker; an asset carries Alpaca's own asset id, which this project has not verified across ticker changes); a verified, complete history of bars for securities that have since been delisted; a verified-complete history of
  corporate actions over 2020-2026. The current asset list is today's universe, not a historical one.

So neither source, alone or together, supplies a point-in-time universe today. Reconstructing one from EDGAR (enumerating historical filers,
parsing removal events, joining prices for delisted tickers) is a data project of its own, not a configuration change.

## 9. Possible future data sources

Documented only: none is adopted, and no cost, licence or coverage is assumed - each would need its terms and coverage verified first.

* **QuantConnect / LEAN** - its US equity data is described as including delisted securities with mapping (ticker-change) files; usable inside
  its environment under its licence terms.
* **CRSP** (typically through WRDS) - the academic standard for survivorship-free US equity returns: permanent identifiers, delisting codes and
  delisting returns. Institutional licence.
* **Commercial market-data vendors (for example Polygon, Databento)** - reference data that can include inactive / delisted tickers, ticker
  events and security definitions. Paid; coverage of delisting outcomes to be verified.
* **Nasdaq / NYSE listing directories** - daily symbol files of what is listed TODAY. Collected forward each day, they become a
  HISTORICAL_SNAPSHOT history from the first collection date onward - never for the past.
* **Index constituent history** (index providers, licensed) - point-in-time membership of a named index. Public reconstructions (for example
  change tables on reference websites) are not authoritative and are not a security master.

The cheapest honest option is forward collection: snapshot the eligible universe at every rebalance from now on. That gives a
HISTORICAL_SNAPSHOT universe for future forward validation; it cannot repair the 2020-2026 backtests.

## 10. Effect on Value + Quality and Momentum

Both declare `FIXED_CURRENT_UNIVERSE` (the 81 stocks of the Stage 3.2 research store, today's large caps), `survivorship_bias: PRESENT`, no
delisted / acquired / bankrupt securities, no historical additions or removals, coverage unknown (`None`). Neither rule, parameter, candidate nor
stored result changes. Applying universe_integrity_v1 to the stored runs, as append-only assessments:

| Strategy | Stored run | Raw selection edge (validation_v3) | Universe integrity | Final selection edge | Overall | Absolute performance |
|---|---|---|---|---|---|---|
| Value + Quality v1.0 | `1660bd50…` (raw edge from methodology evaluation `8f568706…`) | NOT_ESTABLISHED (OOS mean excess +0.11 % per period, t = 0.18, 22 periods) | UNTRUSTED | NOT_ESTABLISHED | FAIL | PASS |
| Momentum v1.0 | `52922e3f…` | NOT_ESTABLISHED (OOS mean excess +1.11 % per period, t = 0.87, 22 periods) | UNTRUSTED | NOT_ESTABLISHED | FAIL | PASS |

No outcome improved, and none could: the cap only lowers. Both raw edges were already NOT_ESTABLISHED, so the cap changes no final status today
(`capped: false`); what changes is that a future rerun on the same fixed universe could not reach ESTABLISHED or PASS however strong its
statistics. For Momentum the bias is not neutral: a price ranking over survivors favours exactly the runs that continued, so even the
unestablished OOS excess is likely overstated. Both strategies remain RESEARCH; no lifecycle changes.

## 11. Adversarial review

Areas reviewed: survivorship leakage, future-membership leakage, delisting handling, acquisition handling, ticker changes, a control masking
survivorship, raw vs final edge confusion, missing declarations, mutation of old runs, UI overstatement, provider limitations, broker isolation.

An independent read-only review tried to break each guarantee with concrete inputs. Findings and what was done:

| # | Severity | Finding | Resolution |
|---|---|---|---|
| H1 | HIGH | a forged record (a complete point-in-time declaration, a bare membership summary, one rebalance "checked", status TRUSTED, final = raw ESTABLISHED, PASS) passed the gate and persisted | fixed: the gate requires the recorded declaration to equal the run's own adapter snapshot, a membership summary with all its fields, and a clean membership check covering every period; the store also requires a registered strategy's run to carry the registered adapter's declaration |
| H2 | HIGH | a methodology evaluation stored after this stage carries the raw v3 edge; with no assessment the Lab read it as final (PASS possible) and called it "a record from before Stage 6.5.1" | fixed: the read view treats a cross-sectional record no assessment covers as UNKNOWN -> UNTRUSTED and caps it (never stored, labelled "not assessed") |
| H3 | HIGH (latent) | the membership check ran one way only: a member that silently never ranked (a later failure without bars) still read TRUSTED; so did a bias declared PRESENT / UNKNOWN and completeness claims with no removal recorded | fixed: both directions (every member is eligible or excluded with a stated reason), NO_DATA exclusions -> LIMITED, TRUSTED needs `survivorship_bias: ABSENT`, completeness claims without any removal -> LIMITED |
| M1 | MEDIUM | `20220601` / `2022-W22-3` were accepted as dates but compared as strings (a delisted member stayed a member) | fixed: `YYYY-MM-DD` only, everywhere (intervals, symbols, snapshots, rebalances, coverage) |
| M2 | MEDIUM | the store re-checked the assessment's statuses only and stored the caller's JSON (forged labels, `point_in_time`, bias, consequence) | fixed: the integrity block, the raw and final edges and the overall verdict must equal the store's own re-derivation field for field |
| L1 | LOW | zero rebalances checked read TRUSTED; snapshot staleness skipped without period dates | fixed: nothing checked -> UNTRUSTED; staleness on every period and selection date |
| L2 | LOW | symbol histories were not validated (overlaps; one symbol held by two securities at once) | fixed: both rejected. Remaining: a dataset keyed by today's ticker for a reused symbol cannot be detected without security ids in the selections (section 12) |
| L3 | LOW | the bias field showed a declared claim as fact; LIMITED / not-assessed records had no list marker | the field reads "Survivorship bias (declared)"; a not-assessed record now reads UNTRUSTED and gets the badge; no separate LIMITED badge (kept concise; the section states it) |
| L4 | LOW | the newest assessment could reference an older evaluation than the one displayed | fixed: the store requires the newest evaluation; the read view ignores an assessment that does not reference it |

Areas with nothing found: future-membership leakage (beyond M1), acquisition handling (beyond M1), the control masking survivorship, mutation
of old runs (triggers cover the new table; no update / delete path; `run_json` never rewritten), provider claims (accurate and hedged),
broker isolation, Value + Quality and Momentum diffs (declaration only), event-driven runs (unaffected).

## 12. Limitations

* TRUSTED is unreachable today: no dataset supplies a membership history. The machinery is tested on synthetic memberships only.
* The gate re-derives the FIXED / UNKNOWN classification and the point-in-time coverage from the recorded declaration, and binds that
  declaration to the run's adapter snapshot (the store, to the registered adapter). It cannot re-run snapshot staleness or the membership
  check without the membership data: it requires the recorded summary and a clean recorded check covering every period, and trusts their
  counts. A record forged consistently in every field, adapter snapshot included, of a strategy without a registered adapter is beyond it.
* A declaration is a claim. The code can check that a supplied membership is consistent and that the ranking used it; it cannot check that the
  membership history itself was not built from survivors.
* Selections carry symbols, not security ids: a dataset whose bars for a reused ticker belong to today's holder cannot be told apart.
* The read view's "not assessed -> UNKNOWN" cap is computed on read and never stored; `assess-universe --persist` records the assessment.
* Delisting returns are not modelled (section 7).
* The cap is a ceiling, not a correction: it does not estimate the size of the bias, and an uncapped TRUSTED result still carries every other
  v3 limitation.
* The universe policy has its own version (`universe_integrity_v1`); the validation methodology stays `validation_v3`.
