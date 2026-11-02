"""
swing_eval/ — Stage 7.8B-1: the versioned evaluation manifest and the OFFLINE evaluator of the swing trend pullback baseline V1 and its control
C1 (DESIGN_78B1_SIP_EVALUATION.md). It runs swing_pullback.plan's four pre-registered runs, unchanged, and nothing else.

Every value below is the operator's confirmed decision lock (STAGE_7_8B1_DECISION_LOCK.md, confirmed 2026-11-02: R8-R17, R22). The strategy's
rules and its pre-registration hash live in swing_pullback/ and are referenced, never changed. A manifest that disagrees with these values,
or with the strategy code, is refused. Changing a value is a new MANIFEST_VERSION, never a change after a real-data result.

Offline by construction: no socket, no database (SQLite is refused while the runs execute), no broker, no harness store; JSON evidence only,
outside the repository. A HISTORICAL dataset needs an operator approval reference AND the approved manifest's sha256 (a procedural record:
the reference's form is checked and the sha256 compared, research_data.APPROVAL_CHECK). A manifest is built only from a CLEAN operator-side
git preflight (swing_eval/preflight.ps1) and pins the commit and its tree (swing_eval/codetree.py).
"""
MANIFEST_VERSION = "stage78b_eval_v1"
EVALUATOR_VERSION = "swing_eval_v1"
LOCK = "STAGE_7_8B1_DECISION_LOCK.md (confirmed by the operator 2026-11-02)"
EVIDENCE_ROOT = r"C:\Data Analyst Py\DA IDX\evidence\stage-78B"          # R17 (outside the repository): the manifest CLI's default parent folder

TRAIN_RATIO = 0.70                               # the harness's chronological split (APPROVED), recorded in the manifest
SIZING = {"equity": 100000.0, "risk_fraction": 0.005, "max_position_pct": 10.0}        # the harness research sizing (APPROVED default)
ARMS = ["RULES"]
EXECUTION = {"entry_policy": "NEXT_BAR_OPEN", "ambiguous_bar_policy": "STOP_FIRST", "time_exit_fill": "CLOSE", "stop_policy": "INVALIDATION"}

R9 = {"fold_min_trades": 20, "min_evaluable_folds": 5, "positive_share": [2, 3], "median_fold_mean_r_min": 0.05, "max_fold_share_of_net": 0.80,
      "gate": "V1 BASE", "reported": ["C1 BASE", "V1 STRESS_2X", "C1 STRESS_2X"]}
R10 = {"segment": "OOS", "r": "r_net", "delta_r_min": 0.05, "both_costs": True, "min_oos_trades": 20, "min_trades": 30,
       "paired_fold_min_trades": 20, "min_paired_folds": 4, "ahead_share": [2, 3], "fold_run": "BASE",
       "bootstrap": {"block": "calendar_month_of_signal", "joint": True, "resamples": 2000, "seed": 78, "percentiles": [5, 50, 95], "role": "descriptive"},
       "interpretation": ["0.05 R is a materiality threshold, not a significance test",
                          "fold consistency includes TRAIN observations: a time-consistency check, not an independent OOS confirmation",
                          "NO_ADDED_VALUE means only that V1 did not show an improvement under the pre-registered mean-R comparison"]}
R11 = ["harness PASS on V1 BASE", "V1 OOS expectancy > 0 at STRESS_2X", "R9 STABLE", "R10 BEATS_CONTROL", "V1 TRAIN-view sensitivity ROBUST",
       "pre-run report accepted and no blocking validation finding"]
R12 = {"treatment": "NOT_MODELLED", "fee_per_share": 0.0, "flag": "REGULATORY_FEES_NOT_MODELLED",
       "reference": {"sec_section_31": [{"from": "2026-04-04", "per_million_usd": 20.60}, {"from": "2025-05-14", "to": "2026-04-03", "per_million_usd": 0.0}],
                     "finra_taf_equity_2026": {"per_share_usd": 0.000195, "max_per_trade_usd": 9.79,
                                               "holiday": {"from": "2026-10-01", "to": "2026-12-31", "per_share_usd": 0.0},
                                               "note": "FINRA's rulebook page still shows an older 0.000166 / 8.30"},
                     "sources": ["SEC fee rate advisory 2026-2", "SEC Release 34-104909", "SR-FINRA-2026-021", "FINRA billing FAQ"]},
       "illustrative_estimate": {"share_price_usd": 50.0, "bps_of_sell_notional": 0.25,
                                 "taf_bps_by_price": {"10": 0.195, "50": 0.039, "100": 0.020, "500": 0.004},
                                 "note": "at the current rates only; not a verified bound for 2016-2026; not representative of every share price"},
       "missing": ["SEC Section 31 rates 2016 -> 2025-05-13", "FINRA TAF rates 2016-2025", "CAT fee schedule"],
       "stress": "the STRESS_2X run is the pre-registered cost stress; it is not claimed to bound the unmodelled fees"}
R13 = {"commission_per_order": 0.0, "status": "ASSUMPTION", "verify": "Alpaca's published fee schedule, before any real run"}
FEE_FLAG_MISSING_HISTORY = "REGULATORY_FEE_HISTORY_MISSING"
