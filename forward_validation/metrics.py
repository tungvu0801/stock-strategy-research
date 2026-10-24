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
                min_trades: int = MIN_TRADES_FOR_EDGE, min_sessions: int = TARGET_SESSIONS) -> Dict[str, Any]:
    """INSUFFICIENT_DATA until at least `min_trades` closed trades AND `min_sessions` healthy sessions exist. Then: expectancy < 0 ->
    EARLY_NEGATIVE; expectancy > 0 with a win rate >= 40 % and a maximum drawdown < 10 % -> EARLY_POSITIVE; anything else -> MIXED. These are
    descriptive labels of a small forward sample, never a forecast; nothing is tuned from them."""
    reasons: List[str] = []
    if closed_trades < min_trades:
        reasons.append(f"{closed_trades} closed trade(s) of the {min_trades} needed")
    if healthy_sessions < min_sessions:
        reasons.append(f"{healthy_sessions} healthy session(s) of the {min_sessions} needed")
    if reasons:
        return {"status": "INSUFFICIENT_DATA", "reasons": reasons, "sample": {"closed_trades": closed_trades, "healthy_sessions": healthy_sessions}}
    e, wr, dd = D(expectancy), D(win_rate_pct), D(max_drawdown_pct)
    if e is None:
        return {"status": "INSUFFICIENT_DATA", "reasons": ["no expectancy could be computed"], "sample": {"closed_trades": closed_trades, "healthy_sessions": healthy_sessions}}
    if e < 0:
        status = "EARLY_NEGATIVE"
    elif e > 0 and (wr is not None and wr >= 40) and (dd is None or dd < 10):
        status = "EARLY_POSITIVE"
    else:
        status = "MIXED"
    return {"status": status, "reasons": [f"expectancy {money(e)} per trade over {closed_trades} trades", f"win rate {pct(wr)} %", f"max drawdown {pct(dd)} %"],
            "sample": {"closed_trades": closed_trades, "healthy_sessions": healthy_sessions}}


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
