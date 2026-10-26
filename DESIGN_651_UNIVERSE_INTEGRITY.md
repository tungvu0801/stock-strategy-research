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
prove that the universe itself was historically unbiased.

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
