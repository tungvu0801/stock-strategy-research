"""
forward_validation/metrics.py - the deterministic arithmetic of Stage 6.1 (pure functions; Decimal money; no I/O, no clock, no broker).

  slippage()         fill vs the persisted reference price (the proposal's reference for entries, the executor's last price for exits)
  trade_pnl()        gross P/L, return % and the descriptive net (fees are not attributable per paper order; see DESIGN_61 section 7)
  excursions()       sampled MFE / MAE of a LONG position from price marks
  win_loss()         wins / losses / flats, averages, win rate and the descriptive forward-paper expectancy
  drawdown()         peak / trough / current drawdown of an (adjusted) equity series
  edge_status()      INSUFFICIENT_DATA until the sample is meaningful, then a conservative qualitative status
  classify_session() HEALTHY | DEGRADED | FAILED | NOT_RUN from what the observer saw (deterministic, documented rules)
  rates()            descriptive conversion rates of the funnel
Nothing here optimizes a threshold or proposes a change.
"""
from __future__ import annotations

from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from forward_validation import CONVERSIONS, MIN_TRADES_FOR_EDGE, TARGET_SESSIONS

Q2 = Decimal("0.01")
Q4 = Decimal("0.0001")


def D(x) -> Optional[Decimal]:
    if x is None or x == "":
        return None
    try:
        d = Decimal(str(x))
    except (InvalidOperation, ValueError):
        return None
    return d if d.is_finite() else None


def money(x: Optional[Decimal]) -> Optional[str]:
    return None if x is None else format(x.quantize(Q2, rounding=ROUND_HALF_UP), "f")


def pct(x: Optional[Decimal], places: Decimal = Q2) -> Optional[str]:
    return None if x is None else format(x.quantize(places, rounding=ROUND_HALF_UP), "f")


# ---------------------------------------------------------------- slippage ------------------------------------------------------------
def slippage(side: str, reference: Any, fill: Any) -> Dict[str, Optional[str]]:
    """Deterministic definition: slippage_absolute = fill - reference for a buy and reference - fill for a sell (positive = adverse, i.e. the
    paper order did worse than its persisted reference price); slippage_bps = slippage_absolute / reference * 10000. No expected execution price
    is invented: an absent reference or fill gives None."""
    ref, px = D(reference), D(fill)
    if ref is None or px is None or ref <= 0:
        return {"reference_price": money(ref), "fill_price": money(px), "slippage_absolute": None, "slippage_bps": None, "definition": "fill - reference (buy) / reference - fill (sell); positive = adverse"}
    diff = (px - ref) if side.lower() == "buy" else (ref - px)
    return {"reference_price": money(ref), "fill_price": money(px), "slippage_absolute": money(diff), "slippage_bps": pct(diff / ref * 10000),
            "definition": "fill - reference (buy) / reference - fill (sell); positive = adverse"}


# ---------------------------------------------------------------- P/L ---------------------------------------------------------------------
def trade_pnl(entry_price: Any, exit_price: Any, qty: Any, fees: Any = None) -> Dict[str, Optional[str]]:
    e, x, q = D(entry_price), D(exit_price), D(qty)
    if e is None or x is None or q is None:
        return {"gross_pnl": None, "fees": money(D(fees)), "net_pnl": None, "return_pct": None, "notional": None}
    gross = (x - e) * q
    f = D(fees)
    net = gross - f if f is not None else gross
    ret = (x - e) / e * 100 if e > 0 else None
    return {"gross_pnl": money(gross), "fees": money(f), "net_pnl": money(net), "return_pct": pct(ret), "notional": money(e * q)}


def excursions(entry_price: Any, marks: Iterable[Any], qty: Any = 1) -> Dict[str, Optional[str]]:
    """Sampled maximum favorable / adverse excursion of a LONG position: the best and worst mark relative to the entry, in price and in
    money for `qty` shares. Marks are the executor's own last prices sampled at the observer cadence (not tick data)."""
    e, q = D(entry_price), D(qty)
    prices = [p for p in (D(m) for m in marks) if p is not None]
    if e is None or not prices:
        return {"mfe_price": None, "mae_price": None, "mfe_pnl": None, "mae_pnl": None, "mfe_pct": None, "mae_pct": None, "marks": len(prices), "basis": "sampled marks"}
    hi, lo = max(prices), min(prices)
    fav, adv = max(Decimal(0), hi - e), max(Decimal(0), e - lo)
    return {"mfe_price": money(hi), "mae_price": money(lo), "mfe_pnl": money(fav * (q or 1)), "mae_pnl": money(-adv * (q or 1)),
            "mfe_pct": pct(fav / e * 100) if e > 0 else None, "mae_pct": pct(-adv / e * 100) if e > 0 else None, "marks": len(prices), "basis": "sampled marks"}


# ---------------------------------------------------------------- win / loss / expectancy ---------------------------------------------
def win_loss(pnls: Iterable[Any]) -> Dict[str, Any]:
    vals = [p for p in (D(x) for x in pnls) if p is not None]
    wins = [p for p in vals if p > 0]
    losses = [p for p in vals if p < 0]
    flats = [p for p in vals if p == 0]
    n = len(vals)
    avg_win = sum(wins) / len(wins) if wins else None
    avg_loss = (sum(losses) / len(losses)).copy_abs() if losses else None
    win_rate = Decimal(len(wins)) / n if n else None
    loss_rate = Decimal(len(losses)) / n if n else None
    expectancy = None
    if n:
        expectancy = (win_rate * (avg_win or Decimal(0))) - (loss_rate * (avg_loss or Decimal(0)))
    return {"closed_trades": n, "win_count": len(wins), "loss_count": len(losses), "flat_count": len(flats), "gross_pnl": money(sum(vals)) if vals else None,
            "win_rate": pct(win_rate * 100) if win_rate is not None else None, "average_win": money(avg_win), "average_loss": money(avg_loss),
            "expectancy": money(expectancy), "expectancy_label": "descriptive forward-paper expectancy per closed trade (win_rate x average_win - loss_rate x average_loss); not a forecast",
            "largest_win": money(max(wins)) if wins else None, "largest_loss": money(min(losses)) if losses else None}


# ---------------------------------------------------------------- drawdown ------------------------------------------------------------
def drawdown(series: Sequence[Tuple[str, Any]]) -> Dict[str, Any]:
    """Peak-to-trough drawdown of an equity series [(at, equity)] in order. Pass ADJUSTED equity (equity minus cumulative external cash flow)
    so deposits and withdrawals never read as trading gains or losses."""
    pts = [(at, D(v)) for at, v in series if D(v) is not None]
    if not pts:
        return {"points": 0, "peak_equity": None, "peak_at": None, "trough_equity": None, "trough_at": None, "current_equity": None, "current_drawdown": None,
                "current_drawdown_pct": None, "max_drawdown": None, "max_drawdown_pct": None, "basis": "adjusted equity (external cash flow removed)"}
    peak, peak_at = pts[0][1], pts[0][0]
    max_dd, max_dd_pct, trough, trough_at = Decimal(0), Decimal(0), pts[0][1], pts[0][0]
    for at, v in pts:
        if v > peak:
            peak, peak_at = v, at
        dd = peak - v
        dd_pct = (dd / peak * 100) if peak > 0 else Decimal(0)
        if dd > max_dd:
            max_dd, max_dd_pct, trough, trough_at = dd, dd_pct, v, at
    cur = pts[-1][1]
    cur_dd = peak - cur
    return {"points": len(pts), "peak_equity": money(peak), "peak_at": peak_at, "trough_equity": money(trough), "trough_at": trough_at, "current_equity": money(cur),
            "current_drawdown": money(cur_dd), "current_drawdown_pct": pct(cur_dd / peak * 100) if peak > 0 else None, "max_drawdown": money(max_dd),
            "max_drawdown_pct": pct(max_dd_pct), "basis": "adjusted equity (external cash flow removed)"}


# ---------------------------------------------------------------- strategy edge status (conservative) ----------------------------------
def edge_status(closed_trades: int, healthy_sessions: int, expectancy: Any, win_rate_pct: Any, max_drawdown_pct: Any, *,
                min_trades: int = MIN_TRADES_FOR_EDGE, min_sessions: int = TARGET_SESSIONS, session_kind: str = "healthy", unverified: int = 0,
                truncated: bool = False) -> Dict[str, Any]:
    """INSUFFICIENT_DATA until at least `min_trades` closed trades AND `min_sessions` sessions of `session_kind` exist (Wave 2 PR C: a run
    passes its strategy days, D-W2-c). Then: expectancy < 0 -> EARLY_NEGATIVE; expectancy > 0 with a win rate >= 40 % and a maximum drawdown
    < 10 % -> EARLY_POSITIVE; anything else -> MIXED. Wave 2 PR C (PF-06, C-52...C-55): a drawdown of None is UNKNOWN - never a pass - so a
    non-negative expectancy reads INSUFFICIENT_DATA "drawdown unknown"; EARLY_POSITIVE also needs `unverified` = 0 (cap 0); a truncated
    statistic is INSUFFICIENT_DATA. These are descriptive labels of a small forward sample, never a forecast; nothing is tuned from them."""
    reasons: List[str] = []
    sample = {"closed_trades": closed_trades, f"{session_kind}_sessions": healthy_sessions}
    if truncated:
        return {"status": "INSUFFICIENT_DATA", "reasons": ["statistics truncated (the row cap was reached)"], "sample": sample}
    if closed_trades < min_trades:
        reasons.append(f"{closed_trades} closed trade(s) of the {min_trades} needed")
    if healthy_sessions < min_sessions:
        reasons.append(f"{healthy_sessions} {session_kind} session(s) of the {min_sessions} needed")
    if reasons:
        return {"status": "INSUFFICIENT_DATA", "reasons": reasons, "sample": sample}
    e, wr, dd = D(expectancy), D(win_rate_pct), D(max_drawdown_pct)
    if e is None:
        return {"status": "INSUFFICIENT_DATA", "reasons": ["no expectancy could be computed"], "sample": sample}
    if e < 0:
        status = "EARLY_NEGATIVE"
    elif dd is None:
        return {"status": "INSUFFICIENT_DATA", "reasons": ["drawdown unknown: no positive or mixed label without a known drawdown"], "sample": sample}
    elif e > 0 and (wr is not None and wr >= 40) and dd < 10:
        status = "EARLY_POSITIVE"
    else:
        status = "MIXED"
    if status == "EARLY_POSITIVE" and unverified:
        return {"status": "INSUFFICIENT_DATA", "reasons": [f"unverified trades: {unverified} closed trade(s) with unverified P/L (no positive label while any is unverified)"],
                "sample": sample}
    return {"status": status, "reasons": [f"expectancy {money(e)} per trade over {closed_trades} trades", f"win rate {pct(wr)} %",
                                          f"max drawdown {pct(dd) if dd is not None else 'unknown'} %"], "sample": sample}


# ---------------------------------------------------------------- session health ------------------------------------------------------
def classify_session(obs: Dict[str, Any]) -> Tuple[str, str]:
    """Deterministic classification of one trading session from the observer's evidence (DESIGN_61 section 4).

    NOT_RUN   no in-session observation at all.
    FAILED    a critical defect: a duplicate broker order, a live-host attempt, an entry allowed under a blocked gate, an unresolved
              reconciliation mismatch / RECONCILIATION_REQUIRED intent / FAILED intent at session end, the early stop, the safety gate
              UNKNOWN at session end, or the broker unavailable for most of the session.
    DEGRADED  a recoverable issue that recovered before session end (stale account / cash-flow data, a reconciliation block, an UNKNOWN
              gate reading, executor cycle errors) or an incomplete window (the open or the close was not observed).
    HEALTHY   the whole window observed, fresh account and cash-flow data throughout, reconciliation healthy, no duplicate, no attention,
              no unresolved execution state, the gate never UNKNOWN.
    Returns (status, notes_code) where notes_code is a '|'-joined list of the reasons."""
    notes: List[str] = []
    if int(obs.get("in_session_samples") or 0) == 0:
        return "NOT_RUN", "NO_OBSERVATION"
    failed = False
    for code, flag in (("DUPLICATE_BROKER_ORDER", obs.get("duplicate_orders")), ("LIVE_HOST_ATTEMPT", obs.get("live_host_attempts")),
                       ("SAFETY_GATE_ALLOWED_UNDER_BAD_STATE", obs.get("gate_violations")), ("EARLY_STOP", obs.get("early_stop")),
                       ("UNRESOLVED_RECONCILIATION", obs.get("unresolved_reconciliation_at_end")), ("UNRESOLVED_EXECUTION_STATE", obs.get("unresolved_execution_at_end")),
                       ("SAFETY_UNKNOWN_AT_END", obs.get("safety_unknown_at_end")), ("BROKER_UNAVAILABLE_PERSISTENT", obs.get("broker_unavailable_persistent"))):
        if flag:
            failed = True
            notes.append(code)
    if failed:
        return "FAILED", "|".join(notes)
    for code, flag in (("ACCOUNT_DATA_STALE_RECOVERED", obs.get("account_stale_samples")), ("CASH_FLOW_STALE_RECOVERED", obs.get("cash_flow_stale_samples")),
                       ("RECONCILIATION_BLOCK_RECOVERED", obs.get("reconciliation_unhealthy_samples")), ("SAFETY_UNKNOWN_RECOVERED", obs.get("safety_unknown_samples")),
                       ("EXECUTOR_CYCLE_ERRORS", obs.get("cycle_errors")), ("REFRESH_FAILURES", obs.get("refresh_failures")), ("BROKER_ERRORS", obs.get("broker_errors")),
                       ("RESTART_DURING_SESSION", obs.get("restarts"))):
        if flag:
            notes.append(code)
    if not obs.get("market_open_seen"):
        notes.append("OPEN_NOT_OBSERVED")
    if not obs.get("market_close_seen"):
        notes.append("CLOSE_NOT_OBSERVED")
    if not obs.get("account_fresh_seen"):
        notes.append("ACCOUNT_DATA_NEVER_FRESH")
    if not obs.get("cash_flow_fresh_seen"):
        notes.append("CASH_FLOW_DATA_NEVER_FRESH")
    if notes:
        return "DEGRADED", "|".join(notes)
    return "HEALTHY", ""


# ---------------------------------------------------------------- funnel conversion rates ---------------------------------------------
def rates(counts: Dict[str, int]) -> Dict[str, Optional[str]]:
    out: Dict[str, Optional[str]] = {}
    for name, den_key, num_key in CONVERSIONS:                                     # (name, FROM stage, TO stage): rate = TO / FROM
        num, den = int(counts.get(num_key) or 0), int(counts.get(den_key) or 0)
        out[name] = pct(Decimal(num) / Decimal(den) * 100) if den > 0 else None
    return out


def classify_safety_fires(evaluations: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Evaluations of one window in time order -> every transition INTO a blocking state (a 'fire'), classified: RECOVERED when a later
    evaluation of the window read TRADING_ALLOWED again, PERSISTENT when the window ended still blocked."""
    fires: List[Dict[str, Any]] = []
    prev: Optional[str] = None
    for i, ev in enumerate(evaluations):
        st = ev.get("state")
        if st != "TRADING_ALLOWED" and st != prev:
            later = [e.get("state") for e in evaluations[i + 1:]]
            fires.append({"at": ev.get("evaluated_at"), "state": st, "reason_codes": list(ev.get("reason_codes") or []), "trigger": ev.get("trigger"),
                          "outcome": "RECOVERED" if "TRADING_ALLOWED" in later else "PERSISTENT"})
        prev = st
    return fires


# ---------------------------------------------------------------- Wave 2 PR B: the session classifier v2 (design WAVE2_PRB_DESIGN_REV3_4 section 5) ----------
# classify_session() above is v1 and stays byte-identical; v2 is a separate pure function. G, H and K are literals (design section 2): never
# read from the environment or the configuration.
G = 360                                   # seconds: the maximum gap, the plausible step and the post-close evidence grace (D-W2-b)
H = 60                                    # seconds: the sample-and-hold cap, the suspect tolerance and the settle margin
K = 3                                     # samples (= 2 plausible steps) confirming a date transition (D-W2-j)
CLASSIFIER_VERSION = "session_classifier_v2_1"                 # Wave 2 PR C (C-65a): the W2-6 duplicate attribution changes the classifier's input
EVIDENCE_READ_ATTEMPTS = 3                # failed evidence reads of one date before escalation (B-44): bounds work only


def hold_coverage(samples: Sequence[Dict[str, Any]], w_open, w_close, cond=None, h: int = H) -> float:
    """Sample-and-hold coverage (B-33): each sample (ordered (at, rowid); `at` a datetime) covers [at, min(next.at, at + h)) within the window;
    the share of the window covered by samples that satisfy `cond` (every sample when None), rounded to 4 decimals."""
    total = (w_close - w_open).total_seconds()
    if total <= 0:
        return 0.0
    covered = 0.0
    for i, s in enumerate(samples):
        if cond is not None and not cond(s):
            continue
        start = max(s["at"], w_open).timestamp()
        end = s["at"].timestamp() + h
        if i + 1 < len(samples):
            end = min(end, samples[i + 1]["at"].timestamp())
        end = min(end, w_close.timestamp())
        covered += max(0.0, end - start)
    return round(min(1.0, covered / total), 4)


def session_gaps(samples: Sequence[Dict[str, Any]], w_open, w_close) -> List[float]:
    """[first - open] + interior gaps + [close - last], in seconds; an empty S is one gap of the whole window (B-33)."""
    if not samples:
        return [(w_close - w_open).total_seconds()]
    ats = [s["at"] for s in samples]
    return [(ats[0] - w_open).total_seconds()] + [(b - a).total_seconds() for a, b in zip(ats, ats[1:])] + [(w_close - ats[-1]).total_seconds()]


def classify_session_v2(ev: Dict[str, Any]) -> Tuple[str, Dict[str, Any]]:
    """The classifier v2 (design section 5), a pure function of the assembled evidence:
      ev = {"market_day", "window": (open, close) | None, "covered", "S": [samples], "end_state": [samples], "suspect_affects": bool,
            "clock_inconsistent": bool, "restarts": [restart times in (open, close]], "failed_rows": {code: count},
            "row_degraded": {code: count}, "unavailable": {source: reason}, "not_settled": bool, "min_coverage": 0.75}
    Samples carry `at` (datetime), `last_cycle_at_dt` (datetime or None) and the persisted fields. Returns (status, result), result =
    {"notes", "failed_codes", "degraded_codes", "measures", "gaps"}; the notes are exactly the code set of the status (B-31)."""
    failed_order = ("DUPLICATE_BROKER_ORDER", "LIVE_HOST_ATTEMPT", "SAFETY_GATE_ALLOWED_UNDER_BAD_STATE", "EARLY_STOP", "UNRESOLVED_RECONCILIATION",
                    "UNRESOLVED_EXECUTION_STATE", "SAFETY_UNKNOWN_AT_END", "BROKER_UNAVAILABLE_PERSISTENT")
    degraded_order = ("COVERAGE_LOW", "ACCOUNT_DATA_COVERAGE_LOW", "CASH_FLOW_DATA_COVERAGE_LOW", "WORKER_COVERAGE_LOW", "OPEN_NOT_OBSERVED", "CLOSE_NOT_OBSERVED",
                      "LONG_GAP", "RESTART_DURING_SESSION", "CALENDAR_NOT_COVERED", "CLOCK_INCONSISTENT", "END_STATE_UNKNOWN", "EVIDENCE_NOT_SETTLED",
                      "EVIDENCE_UNREADABLE", "CALENDAR_CONFLICT", "ACCOUNT_DATA_STALE_RECOVERED", "CASH_FLOW_STALE_RECOVERED", "RECONCILIATION_BLOCK_RECOVERED",
                      "SAFETY_UNKNOWN_RECOVERED", "ACCOUNT_DATA_NEVER_FRESH", "CASH_FLOW_DATA_NEVER_FRESH", "EXECUTOR_CYCLE_ERRORS", "REFRESH_FAILURES", "BROKER_ERRORS")
    min_cov = float(ev.get("min_coverage", 0.75))

    def worker_fresh(s):                                                           # loop running, a recent completed cycle, the persisted threshold (N-3)
        lc, st = s.get("last_cycle_at_dt"), s.get("worker_stale_after_s")
        if s.get("loop_running") is not True or lc is None or st is None:
            return False
        return 0 <= (s["at"] - lc).total_seconds() <= float(st)

    failed: List[str] = [c for c in ("DUPLICATE_BROKER_ORDER", "LIVE_HOST_ATTEMPT", "SAFETY_GATE_ALLOWED_UNDER_BAD_STATE", "EARLY_STOP")
                         if int((ev.get("failed_rows") or {}).get(c) or 0) > 0]
    end = list(ev.get("end_state") or [])
    if any(int(s.get("rr_intents") or 0) > 0 or int(s.get("attention_positions") or 0) > 0 for s in end):
        failed.append("UNRESOLVED_RECONCILIATION")                                 # positive evidence counts even when the other count is NULL
    if any(int(s.get("failed_intents") or 0) > 0 for s in end):
        failed.append("UNRESOLVED_EXECUTION_STATE")
    if any(s.get("safety_state") == "UNKNOWN" for s in end):
        failed.append("SAFETY_UNKNOWN_AT_END")
    S = list(ev.get("S") or [])
    if S and 2 * sum(1 for s in S if any("broker unavailable" in str(a) for a in (s.get("attention") or []))) > len(S):
        failed.append("BROKER_UNAVAILABLE_PERSISTENT")                             # by sample count (U-23)
    deg: List[str] = []
    measures: Dict[str, Any] = {}
    gaps: List[float] = []
    w = ev.get("window") if ev.get("market_day") else None
    if ev.get("unavailable"):
        deg.append("EVIDENCE_UNREADABLE")
    end_unknown = any(any(s.get(k) is None for k in ("rr_intents", "attention_positions", "failed_intents", "safety_state")) for s in end)
    if end_unknown:
        deg.append("END_STATE_UNKNOWN")
    if ev.get("not_settled"):
        deg.append("EVIDENCE_NOT_SETTLED")
    if w is None:
        deg.append("CALENDAR_CONFLICT")
    else:
        o, c = w
        measures = {"obs_cov": hold_coverage(S, o, c), "fresh_cov": hold_coverage(S, o, c, lambda s: s.get("account_status") == "FRESH"),
                    "cf_cov": hold_coverage(S, o, c, lambda s: s.get("cash_flow_status") == "FRESH"), "worker_cov": hold_coverage(S, o, c, worker_fresh)}
        gaps = session_gaps(S, o, c)
        for code, key in (("COVERAGE_LOW", "obs_cov"), ("ACCOUNT_DATA_COVERAGE_LOW", "fresh_cov"), ("CASH_FLOW_DATA_COVERAGE_LOW", "cf_cov"), ("WORKER_COVERAGE_LOW", "worker_cov")):
            if measures[key] < min_cov:
                deg.append(code)
        if gaps[0] > G:
            deg.append("OPEN_NOT_OBSERVED")
        if gaps[-1] > G:
            deg.append("CLOSE_NOT_OBSERVED")
        if any(g > G for g in gaps[1:-1]):
            deg.append("LONG_GAP")
        if ev.get("restarts"):
            deg.append("RESTART_DURING_SESSION")
        if not ev.get("covered", True):
            deg.append("CALENDAR_NOT_COVERED")
        if ev.get("clock_inconsistent"):
            deg.append("CLOCK_INCONSISTENT")
        for code, pred in (("ACCOUNT_DATA_STALE_RECOVERED", lambda s: s.get("account_status") in ("STALE", "UNAVAILABLE", "DISABLED")),
                           ("CASH_FLOW_STALE_RECOVERED", lambda s: s.get("cash_flow_status") in ("STALE", "UNAVAILABLE", "AMBIGUOUS")),
                           ("RECONCILIATION_BLOCK_RECOVERED", lambda s: s.get("reconciliation_healthy") is False),
                           ("SAFETY_UNKNOWN_RECOVERED", lambda s: s.get("safety_state") == "UNKNOWN")):
            if any(pred(s) for s in S):
                deg.append(code)
        if S and not any(s.get("account_status") == "FRESH" for s in S):
            deg.append("ACCOUNT_DATA_NEVER_FRESH")
        if S and not any(s.get("cash_flow_status") == "FRESH" for s in S):
            deg.append("CASH_FLOW_DATA_NEVER_FRESH")
        for code in ("EXECUTOR_CYCLE_ERRORS", "REFRESH_FAILURES", "BROKER_ERRORS"):
            if int((ev.get("row_degraded") or {}).get(code) or 0) > 0:
                deg.append(code)
    failed_codes = [c for c in failed_order if c in failed]
    degraded_codes = [c for c in degraded_order if c in deg]
    if failed_codes:
        status, notes = "FAILED", list(failed_codes)
    elif w is None:
        status, notes = "DEGRADED", list(degraded_codes)                           # an OPEN row for a non-market day (5.1 item 2)
    elif not S and not ev.get("suspect_affects") and not ev.get("unavailable") and not end_unknown:
        status = "NOT_RUN"                                                         # B-20: an unsettled late path keeps NOT_RUN; the code stays in the record
        degraded_codes = [c for c in degraded_codes if c in ("EVIDENCE_NOT_SETTLED", "CALENDAR_NOT_COVERED")]
        notes = ["NO_OBSERVATION"] + (["CALENDAR_NOT_COVERED"] if not ev.get("covered", True) else [])
    elif degraded_codes:
        status, notes = "DEGRADED", list(degraded_codes)
    else:
        status, notes = "HEALTHY", []
    return status, {"notes": notes, "failed_codes": failed_codes, "degraded_codes": degraded_codes, "measures": measures, "gaps": gaps}


# ---------------------------------------------------------------- Wave 2 PR C: the bot-only drawdown (design 9) -------------------------
def price_age_unknown(a, b, ref0, resets: Sequence[Tuple[Any, Any]], g: int = G) -> Dict[str, Any]:
    """C-41 / C-42 (semantic 1): the price age a(t) = t - ref(t) over an in-position interval [a, b] (datetimes), measured CONTINUOUSLY - not on
    a grid. `ref0` is the reference at a (the window open, the entry observation or the latest price evidence, whichever is newest); `resets`
    are (time, new_ref) events in (a, b] (a mark carrying a newer last_price_at, or the entry observation). The age peaks just before a reset
    and at b. UNKNOWN <=> the age exceeds G at any instant (G counted once, no stretch, no tolerance). Returns {unknown, max_age_s, fresh_s,
    total_s}: fresh_s is the in-position time with age <= G (C-44: information only - it never decides 'known')."""
    ref = ref0
    t0 = a
    max_age, fresh = 0.0, 0.0

    def span(lo, hi, r):                                                       # the age over [lo, hi] rises linearly from lo - r to hi - r
        lo_age, hi_age = (lo - r).total_seconds(), (hi - r).total_seconds()
        within = max(0.0, min(hi_age, float(g)) - max(lo_age, 0.0)) if hi_age > max(lo_age, 0.0) else 0.0
        return max(lo_age, hi_age), min(within, (hi - lo).total_seconds())
    for at, new_ref in sorted((x for x in resets if a < x[0] <= b), key=lambda x: x[0]):
        m, f = span(t0, at, ref)
        max_age, fresh = max(max_age, m), fresh + f
        if new_ref is not None and new_ref > ref:
            ref = new_ref
        t0 = at
    m, f = span(t0, b, ref)
    max_age, fresh = max(max_age, m), fresh + f
    total = max(0.0, (b - a).total_seconds())
    return {"unknown": max_age > g, "max_age_s": max_age, "fresh_s": round(min(fresh, total), 3), "total_s": total}


def bot_drawdown_pct(base: Any, values: Sequence[Any]) -> Optional[str]:
    """C-45: the maximum PERCENTAGE drawdown of the bot-only value curve V(t) over the grid, computed directly: peak_t = max(B, max V(s) for
    s <= t); dd_pct(t) = 100 * (peak_t - V(t)) / peak_t. Not drawdown() (W2-N31: 100 -> 90 -> 200 -> 189 is 10.00 %, not 5.50 %). None when a
    value is missing (the caller passes UNKNOWN as None: never 0, C-57)."""
    b = D(base)
    vals = [D(v) for v in values]
    if b is None or b <= 0 or any(v is None for v in vals):
        return None
    peak, worst = b, Decimal(0)
    for v in vals:
        peak = max(peak, v)
        worst = max(worst, (peak - v) / peak * 100)
    return pct(worst)
