"""
decision_flow/view.py - the Stage 6.10C payloads (read only; DESIGN_610C_DECISION_FLOW.md sections 3-6).

  latest()        the latest setup evaluation cycle: its summary, one row per ticker, Closest to READY and the dynamic-universe status (when
                  today has no cycle, the most recent one with stale=true)
  cycle(at)       one cycle reconstructed: the scheduler batch at or before `at`, with the executor runs of its window
  history(date)   one New York trading date: the day counts and the cycle list (newest first)
  explain(...)    why a ticker did (not) trade at a time given in ET, PT or UTC: its evaluation at or before that time (within 10 minutes) with
                  the mode, the risk gate and the executor around it, and one plain paragraph
  universe()      the inactive dynamic-universe plan (dynamic_universe.plan.status), or 'not available'

An evaluation cycle is one scheduler batch: the SCHEDULED setup_evaluations rows sharing one evaluated_at with at least 2 distinct symbols (a
symbol twice in one stamp keeps its lower rowid). Single-symbol SCHEDULED rows (the Trader View stream) and SUBSCRIBE rows are off-cycle
evaluations. The executor runs of a cycle are those started in [cycle, next cycle), at most 10 minutes; the executor result of a READY ticker
is attached by that time window, never by id. Times are New York time (setups.market_hours.to_et) with UTC alongside.

Nothing here writes, migrates, evaluates, calls safety.current(), requests market data or calls a broker. The one in-memory read is the shared
market-data cache of an already running Trader View stream, for the latest cycle's price (never quote() / quotes(): no request).
"""
from __future__ import annotations

import bisect as BS
import re
import threading
from collections import Counter
from datetime import date as Date
from datetime import datetime, timedelta, timezone
from time import monotonic
from typing import Any, Callable, Dict, List, Optional, Tuple

import config
from api.validation import validate_symbol
from autorun import POLICY_LABELS
from autorun.policy import settings_from
from autorun.session import classify
from decision_flow import (AWAITING_EXECUTOR, BLOCKED, CONTROL_LABELS, DECISIONS, DISCLAIMER, ENTRIES_PER_CYCLE, ENTRY_CONTROLS_NOT_EVALUATED, EVALUATION_ERROR,
                           INVALIDATED, MODE_LABELS, NO_EXECUTOR_RESULT, NO_SETUP, NOT_AVAILABLE, ORDER_INTENT, PAPER_AUTO_OFF, PAUSED, PRICE_HISTORY_TEXT, READY,
                           SAFETY_STATE_LABELS, SESSION_LABELS, SETUP_NAME, SETUP_NOTE, SETUP_TYPE, SETUP_WATCH, SKIPPED, STATE_LABELS, UNIVERSE_NOT_AVAILABLE,
                           VERSION, WATCHING, legend, text_for)
from decision_flow import conditions as C
from decision_flow.store import DecisionFlowStore, Reader
from setups.market_hours import session_date, to_et

UTC = timezone.utc
CYCLE_MIN_SYMBOLS = 2                    # a scheduler batch: >= 2 distinct symbols in one SCHEDULED stamp
CYCLE_WINDOW_CAP_S = 600                 # the executor window of a cycle ends at the next cycle, at most this long after it
EXPLAIN_MAX_AGE_S = 600                  # explain: the evaluation must be at most 10 minutes before the asked time
PRICE_MAX_AGE_S = 60                     # a cached quote older than this (by its trade time) is not shown
LATEST_TTL_S, HISTORY_TTL_S, CACHE_MAX = 5.0, 30.0, 32
SCAN_CHUNK, MAX_SCAN_CHUNKS = 256, 40    # cycle search: rows per step and steps (bounded; never from the table start)
HISTORY_LIMIT, HISTORY_MAX = 60, 400
YEARS = (2000, 2100)                     # the dates a question may name: year 1 or 9999 overflows the time arithmetic
TZ_NAMES = {"ET": "America/New_York", "PT": "America/Los_Angeles", "UTC": "UTC"}
EVAL_COLS = ("evaluation_id, evaluated_at, symbol, setup_type, setup_id, prior_state, resulting_state, decision, reason_code, reason_text, inputs_json, "
             "freshness_json, trigger")
RUN_COLS = "run_id, started_at, finished_at, trigger, mode, result, summary_json, error"
DAY_COLS = ("evaluated_at, symbol, trigger, setup_id, resulting_state, decision, json_extract(inputs_json, '$.stretch') AS stretch, "
            "json_extract(inputs_json, '$.demand') AS demand, json_extract(inputs_json, '$.confidence_status') AS confidence_status, "
            "json_extract(freshness_json, '$.blockers') AS blockers")
SKIPPED_BASIS = ("READY evaluations (any trigger) made while the mode was Monitor only, the mode at each evaluation read from execution_mode_events; "
                 "forward capture records the same case as SKIPPED with PAPER_AUTO_OFF.")
BLOCKED_BASIS = ("Cycle tickers whose READY setup the executor blocked in the cycle's window: one per ticker per cycle (the cycle table's decision), "
                 "not one per executor run.")
_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_TIME = re.compile(r"^\d{2}:\d{2}(:\d{2})?$")
_HOSTISH = re.compile(r"(?i)https?://\S+|[\w.-]*alpaca[\w.-]*")
_CACHE: Dict[tuple, Tuple[float, Dict[str, Any]]] = {}
_CACHE_LOCK = threading.Lock()


# ================================================================ small helpers ==============================================================
def _iso(dt: datetime) -> str:
    return dt.astimezone(UTC).isoformat(timespec="seconds")


def _parse(ts: Any) -> Optional[datetime]:
    if not ts:
        return None
    try:
        d = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except ValueError:
        return None
    return d if d.tzinfo else d.replace(tzinfo=UTC)


def _shift(ts: str, seconds: float) -> str:
    return _iso(_parse(ts) + timedelta(seconds=seconds))


def _et(ts: Any) -> Optional[str]:
    """New York time of a stored UTC timestamp; None without a time-zone database (never a UTC guess)."""
    d = _parse(ts)
    et = to_et(d) if d is not None else None
    return et.strftime("%Y-%m-%d %H:%M:%S ET") if et else None


def _day(ts: Any) -> Optional[str]:
    d = _parse(ts)
    sd = session_date(d) if d is not None else None
    return sd.isoformat() if sd else None


def cycle_id(at: Optional[str]) -> Optional[str]:
    """The derived cycle id: 'SE-' + the batch stamp in compact UTC (the engine persists no cycle id)."""
    d = _parse(at)
    return "SE-" + d.astimezone(UTC).strftime("%Y%m%dT%H%M%SZ") if d else None


def _zone(tz: str):
    if tz == "UTC":
        return UTC
    if tz == "ET":
        et = to_et(datetime.now(UTC))
        return et.tzinfo if et else None
    try:
        from zoneinfo import ZoneInfo
        return ZoneInfo(TZ_NAMES[tz])
    except Exception:  # noqa: BLE001 - no time-zone database: the caller says so
        return None


def _day_window(day: str) -> Tuple[str, str]:
    """[00:00, 24:00) of a New York calendar date, in UTC."""
    tz = _zone("ET") or UTC
    d = Date.fromisoformat(day)
    nxt = d + timedelta(days=1)
    return _iso(datetime(d.year, d.month, d.day, tzinfo=tz)), _iso(datetime(nxt.year, nxt.month, nxt.day, tzinfo=tz))


def _clean(text: Any, limit: int = 300) -> Optional[str]:
    """A stored free text as shown: no URL or broker host name, bounded."""
    return None if text is None else _HOSTISH.sub("[host]", str(text))[:limit]


def _ints(v: Any) -> int:
    try:
        return int(v or 0)
    except (TypeError, ValueError):
        return 0


# ================================================================ the cache (production calls only) =========================================
def _cached(key: tuple, ttl: float, store: DecisionFlowStore, build: Callable[[], Dict[str, Any]]) -> Dict[str, Any]:
    """A small TTL cache keyed by the newest rowid of every table read: a new row is never hidden behind it."""
    with store.read() as r:
        full = key + (r.marks(),)
    t = monotonic()
    with _CACHE_LOCK:
        hit = _CACHE.get(full)
        if hit is not None and t - hit[0] < ttl:
            return hit[1]
    out = build()
    with _CACHE_LOCK:
        _CACHE[full] = (t, out)
        while len(_CACHE) > CACHE_MAX:
            _CACHE.pop(next(iter(_CACHE)))
    return out


# ================================================================ the Trader View stream (read, never built) =================================
def _stream_instance() -> Any:
    """The Trader View stream ONLY if it already exists: stream() would build one (and its market-data clients)."""
    try:
        from api.routes import trader_view as RT
        return RT.STREAM.get("instance")
    except Exception:  # noqa: BLE001
        return None


def _live_symbols(stream: Any) -> List[str]:
    if stream is not None:
        try:
            return list(stream.watch_symbols())
        except Exception:  # noqa: BLE001 - fall back to the file itself
            pass
    try:
        from scanner.watchlist import load_watchlist
        return list(load_watchlist())
    except Exception:  # noqa: BLE001
        return []


def _plan_status(live: List[str]) -> Dict[str, Any]:
    from dynamic_universe import plan as DU          # lazily: no live path depends on the inactive universe
    return DU.status(live)


def _universe_status(stream: Any) -> Dict[str, Any]:
    try:
        out = _plan_status(_live_symbols(stream))
        if not isinstance(out, dict):
            raise TypeError("the universe status is not a mapping")
        return {**out, "available": True}
    except Exception:  # noqa: BLE001 - the inactive universe never breaks the decision flow
        return {"available": False, "text": UNIVERSE_NOT_AVAILABLE}


def _price_na(text: str = NOT_AVAILABLE) -> Dict[str, Any]:
    return {"value": None, "as_of": None, "text": text}


def _cached_price(stream: Any, symbol: str, now: datetime) -> Dict[str, Any]:
    """The last quote in the shared market-data cache (RealMarketData._quotes), read without a request: status OK and a trade time at most 60 s
    old, else Not available."""
    cache = getattr(getattr(stream, "market", None), "_quotes", None)
    hit = cache.get(symbol) if isinstance(cache, dict) else None
    q = hit[1] if isinstance(hit, tuple) and len(hit) >= 2 else None
    if q is None or getattr(q, "status", None) != "OK" or getattr(q, "price", None) is None or getattr(q, "ts", None) is None:
        return _price_na()
    age = now.timestamp() - float(q.ts)
    if age > PRICE_MAX_AGE_S or age < -5:
        return _price_na(f"{NOT_AVAILABLE} (the cached quote is older than {PRICE_MAX_AGE_S} s)" if age > 0 else NOT_AVAILABLE)
    return {"value": float(q.price), "as_of": _iso(datetime.fromtimestamp(float(q.ts), UTC)),
            "text": f"{float(q.price):.2f} (last cached quote, {max(0, int(age))} s old; no request made)"}


# ================================================================ point-in-time context (the last persisted row <= t) ======================
def _mode_at(r: Reader, t: str) -> Dict[str, Any]:
    """Paper Auto at t: the to_mode of the last mode event <= t (MONITOR_ONLY before any). environment_json is never selected."""
    ev = r.last_before("execution_mode_events", t, "at, to_mode, actor") or {}
    mode = ev.get("to_mode") or "MONITOR_ONLY"
    pol = settings_from(config)["effective_policy"]
    return {"mode": mode, "on": mode == "PAPER_AUTO", "label": MODE_LABELS.get(mode, mode), "since": ev.get("at"), "since_et": _et(ev.get("at")),
            "changed_by": ev.get("actor"), "policy": pol, "policy_label": POLICY_LABELS.get(pol, pol)}


def _risk_at(r: Reader, t: str) -> Dict[str, Any]:
    """The risk gate, the auto-run preflight and the reconciliation as the last persisted rows at or before t (never safety.current())."""
    sv = r.last_before("execution_safety_evaluations", t, "evaluated_at, trigger, state, allowed_new_entry, reason_codes_json")
    pf = r.last_before("autorun_preflights", t, "at, status, checks_json")
    fv = r.last_before("forward_validation_samples", t, "at, reconciliation_healthy, attention_json")
    codes = [str(c) for c in (C.loads((sv or {}).get("reason_codes_json"), []) or []) if c]
    checks = [c for c in (C.loads((pf or {}).get("checks_json"), []) or []) if isinstance(c, dict)]
    healthy = (fv or {}).get("reconciliation_healthy")
    return {"state": (sv or {}).get("state"), "label": SAFETY_STATE_LABELS.get((sv or {}).get("state"), NOT_AVAILABLE),
            "allowed_new_entry": bool(sv["allowed_new_entry"]) if sv else None, "reason_codes": codes, "reason_text": [text_for(c) for c in codes],
            "evaluated_at": (sv or {}).get("evaluated_at"), "evaluated_at_et": _et((sv or {}).get("evaluated_at")),
            "preflight": {"status": (pf or {}).get("status"), "at": (pf or {}).get("at"), "failing": [str(c.get("id")) for c in checks if c.get("status") == "FAIL"],
                          "warnings": [str(c.get("id")) for c in checks if c.get("status") == "WARN"]},
            "reconciliation": {"healthy": None if healthy is None else bool(healthy),
                               "attention": [_clean(a, 160) for a in (C.loads((fv or {}).get("attention_json"), []) or [])][:10], "sampled_at": (fv or {}).get("at")}}


# ================================================================ the executor in a window =================================================
def _runs(r: Reader, lo: str, hi: str) -> List[Dict[str, Any]]:
    out = []
    for x in r.window("execution_runs", lo, hi, RUN_COLS):
        s = C.loads(x.get("summary_json"), {})
        out.append({"run_id": x["run_id"], "at": x["started_at"], "finished_at": x.get("finished_at"), "trigger": x.get("trigger"), "mode": x.get("mode"),
                    "result": x.get("result"), "error_type": C.error_type(x["error"]) if x.get("error") else None, "summary": s if isinstance(s, dict) else {}})
    return sorted(out, key=lambda run: run["at"])


def _captured(r: Reader, lo: str, hi: str) -> Dict[Tuple[str, str], List[Dict[str, Any]]]:
    """Every entry control's PASS / BLOCK per (run, symbol), from the Stage 6.10A forward-capture EXECUTOR_CYCLE rows (indexed by date and time)."""
    days = sorted({d for d in (_day(lo), _day(hi)) if d})
    if not days:
        return {}
    rows = r.query(f"SELECT run_id, symbol, controls_json FROM forward_capture_decisions WHERE trading_date IN ({','.join('?' * len(days))}) "
                   "AND decided_at >= ? AND decided_at < ? AND source = 'EXECUTOR_CYCLE'", (*days, lo, hi))
    out: Dict[Tuple[str, str], List[Dict[str, Any]]] = {}
    for x in rows:
        cs = C.loads(x.get("controls_json"), None)
        if isinstance(cs, list) and cs and x.get("run_id"):
            out[(x["run_id"], x["symbol"])] = [{"id": c.get("control_id"), "label": CONTROL_LABELS.get(str(c.get("control_id")), c.get("control_id")),
                                                "result": c.get("result"), "reason": _clean(c.get("reason"))} for c in cs if isinstance(c, dict)]
    return out


def _run_results(run: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    """Per symbol, what one executor run did with a READY setup: BLOCKED (its blocking controls), SKIPPED (the entry limit) or ENTRY_INTENT."""
    e = run["summary"].get("entries")
    e = e if isinstance(e, dict) else {}
    base = {"run_id": run["run_id"], "at": run["at"]}
    out: Dict[str, Dict[str, Any]] = {}
    for ev in e.get("evaluations") or []:
        if not isinstance(ev, dict) or not ev.get("symbol"):
            continue
        res = str(ev.get("result") or "")
        reasons = [x for x in ev.get("reasons") or [] if isinstance(x, dict)]
        blocked = res == "BLOCKED"
        out[str(ev["symbol"])] = {**base, "result": res, "execution_id": ev.get("execution_id"), "blocked_by": [str(x.get("control")) for x in reasons] if blocked else [],
                                  "controls": [{"id": x.get("control"), "label": CONTROL_LABELS.get(str(x.get("control")), x.get("control")), "result": "BLOCK",
                                                "reason": _clean(x.get("text"))} for x in reasons] if blocked else [],
                                  "text": _clean((reasons[0] if reasons else {}).get("text"))}
    for sym, ids in (e.get("blocked") or {}).items():                    # runs written before entries.evaluations existed: the blocking ids only
        ids = [str(i) for i in ids or []]
        out.setdefault(str(sym), {**base, "result": "BLOCKED", "execution_id": None, "blocked_by": ids, "text": None,
                                  "controls": [{"id": i, "label": CONTROL_LABELS.get(i, i), "result": "BLOCK", "reason": None} for i in ids]})
    for sym in e.get("submitted") or []:
        if out.get(str(sym), {}).get("result") != "ENTRY_INTENT":
            out[str(sym)] = {**base, "result": "ENTRY_INTENT", "execution_id": None, "blocked_by": [], "controls": [], "text": None}
    return out


def _merge(per_run: List[Tuple[Dict[str, Any], Dict[str, Dict[str, Any]]]], captured: Dict[Tuple[str, str], List[Dict[str, Any]]]) -> Dict[str, Dict[str, Any]]:
    """One executor result per symbol over a window (runs in time order): an entry intent wins, else the latest run's result."""
    out: Dict[str, Dict[str, Any]] = {}
    for run, results in per_run:
        for sym, res in results.items():
            prev = out.get(sym)
            if prev is not None and prev["result"] == "ENTRY_INTENT":
                prev["runs"] += 1
                continue
            full = captured.get((run["run_id"], sym))
            if full:                                                           # a blocking id the captured list lacks (DUPLICATE_ENTRY is decided before it)
                have = {c.get("id") for c in full}
                full = full + [c for c in res["controls"] if c.get("id") not in have]
            out[sym] = {**res, "controls": full if full else res["controls"], "full": bool(full), "runs": (prev or {}).get("runs", 0) + 1}
    return out


def _intents(r: Reader, xids: List[Any]) -> List[Dict[str, Any]]:
    ids = sorted({str(x) for x in xids if x})
    out: List[Dict[str, Any]] = []
    for k in range(0, len(ids), 200):                                          # primary-key lookups only
        part = ids[k:k + 200]
        out += r.query(f"SELECT execution_id, symbol, state, attempt_count, filled_qty FROM execution_intents WHERE execution_id IN ({','.join('?' * len(part))})",
                       part)
    return out


def _window_end(at: str, next_at: Optional[str]) -> str:
    cap = _shift(at, CYCLE_WINDOW_CAP_S)
    return min(next_at, cap) if next_at else cap


# ================================================================ cycles (scheduler batches) ================================================
def _batch(rows: List[Dict[str, Any]]) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """(cycle rows, off-cycle rows) of one stamp: the SCHEDULED rows, one per symbol (the lower rowid), if they cover >= 2 symbols."""
    keep: Dict[str, Dict[str, Any]] = {}
    off: List[Dict[str, Any]] = []
    for x in sorted(rows, key=lambda y: y["rid"]):
        if x.get("trigger") == "SCHEDULED" and x["symbol"] not in keep:
            keep[x["symbol"]] = x
        else:
            off.append(x)
    if len(keep) < CYCLE_MIN_SYMBOLS:
        return [], sorted(rows, key=lambda y: y["rid"])
    return list(keep.values()), off


def _origin(trigger: Optional[str], in_cycle: bool) -> str:
    if in_cycle:
        return "SCHEDULER_CYCLE"
    if trigger in ("SCHEDULED", "REFRESH"):
        return "TRADER_VIEW"
    return str(trigger or "")


def _stamp_rows(r: Reader, at: str) -> List[Dict[str, Any]]:
    return r.window("setup_evaluations", at, at, EVAL_COLS, hi_inclusive=True)


def _qualified(seen: Dict[str, set]) -> List[str]:
    return [s for s, syms in seen.items() if len(syms) >= CYCLE_MIN_SYMBOLS]


def _cycle_before(r: Reader, t: Optional[str]) -> Optional[str]:
    """The newest batch stamp <= t (t None: the newest of all), walking back in bounded chunks from the bisection point."""
    first, last = r.first_rowid("setup_evaluations"), r.last_rowid("setup_evaluations")
    if last is None:
        return None
    if t is None:
        pos = last + 1
    else:
        b = r.bisect("setup_evaluations", t)
        pos = (b if b is not None else first - 1) + 2 * SCAN_CHUNK + 1         # include rows of earlier stamps written late
    seen: Dict[str, set] = {}
    found = False
    for _ in range(MAX_SCAN_CHUNKS):
        rows = r.chunk("setup_evaluations", before=pos, cols="symbol, trigger", limit=SCAN_CHUNK)
        if not rows:
            break
        for x in rows:
            if x["trigger"] == "SCHEDULED" and (t is None or x["t"] <= t):
                seen.setdefault(x["t"], set()).add(x["symbol"])
        pos = rows[-1]["rid"]
        if _qualified(seen):
            if found:
                break
            found = True                                                       # one more chunk: a newer stamp's rows can sit a little further back
    q = _qualified(seen)
    return max(q) if q else None


def _cycle_after(r: Reader, t: str) -> Optional[str]:
    """The first batch stamp after t, within the window cap (bounded forward chunks from the bisection point)."""
    first = r.first_rowid("setup_evaluations")
    if first is None:
        return None
    end_t = _shift(t, CYCLE_WINDOW_CAP_S)
    b = r.bisect("setup_evaluations", t)
    pos = (b if b is not None else first - 1) - SCAN_CHUNK
    seen: Dict[str, set] = {}
    found = False
    for _ in range(MAX_SCAN_CHUNKS):
        rows = r.chunk("setup_evaluations", after=pos, cols="symbol, trigger", limit=SCAN_CHUNK)
        if not rows:
            break
        for x in rows:
            if x["trigger"] == "SCHEDULED" and t < x["t"] <= end_t:
                seen.setdefault(x["t"], set()).add(x["symbol"])
        pos = rows[-1]["rid"]
        if _qualified(seen):
            if found:
                break
            found = True
        elif all(x["t"] > end_t for x in rows):
            break
    q = _qualified(seen)
    return min(q) if q else None


# ================================================================ one ticker, one cycle ======================================================
def _ticker_risk(exe: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    if exe is None:
        return {"evaluated": False, "text": ENTRY_CONTROLS_NOT_EVALUATED, "controls": [], "blocked_by": [], "source": None}
    if exe["result"] == "SKIPPED":
        return {"evaluated": False, "text": text_for(ENTRIES_PER_CYCLE), "controls": [], "blocked_by": [], "source": "execution_runs", "run_id": exe.get("run_id"),
                "at": exe.get("at")}
    full = bool(exe.get("full"))
    if exe["result"] == "ENTRY_INTENT":
        text = "Every entry control passed" + ("" if full else " (the run summary keeps only blocking controls)") + "; a paper entry intent was recorded."
    else:
        text = "Blocked by " + ", ".join(CONTROL_LABELS.get(i, i) for i in exe.get("blocked_by") or []) + \
               ("." if full else " (the run summary keeps only the controls that blocked).")
    return {"evaluated": True, "text": text, "controls": list(exe.get("controls") or []), "blocked_by": list(exe.get("blocked_by") or []),
            "source": "forward_capture_decisions" if full else "execution_runs", "run_id": exe.get("run_id"), "at": exe.get("at")}


def _no_trade(dec: Dict[str, str], reason: str, x: Dict[str, Any], risk: Dict[str, Any], past: bool) -> Optional[str]:
    k = dec["decision_detail"]
    dist = x["distance"]["text"]
    if k == ORDER_INTENT:
        return None
    if k == AWAITING_EXECUTOR:
        return NO_EXECUTOR_RESULT if past else f"Not yet: {text_for(AWAITING_EXECUTOR)}"
    if k == PAPER_AUTO_OFF:
        return "No trade: the setup was READY, but Paper Auto was off (Monitor only), so it was not sent to the executor."
    if k == ENTRIES_PER_CYCLE:
        return f"No trade this cycle: {text_for(ENTRIES_PER_CYCLE)}"
    if dec["decision"] == BLOCKED:
        return "No trade: blocked by " + ", ".join(CONTROL_LABELS.get(i, i) for i in risk["blocked_by"]) + "."
    if k == SETUP_WATCH:
        return f"No trade: the setup is on WATCH, not READY. {reason} {dist}"
    if k in (INVALIDATED, PAUSED, EVALUATION_ERROR):
        return f"No trade: {reason}"
    return f"No trade: no active setup. {reason} {dist}"


def _ticker_row(ev: Dict[str, Any], origin: str, paper_auto: bool, exe: Optional[Dict[str, Any]], price: Dict[str, Any], *, past: bool) -> Dict[str, Any]:
    """One evaluation as the per-ticker row of DESIGN_610C section 6.1."""
    x = C.explain(ev)
    raw = C.loads(ev.get("inputs_json"), {})
    raw = raw if isinstance(raw, dict) else {}
    state = ev.get("resulting_state") or "NONE"
    # a PAUSED / ERROR evaluation keeps its stored READY state, and the executor still reads that open READY episode: what it did in the window is
    # attached all the same (decide() lets it win over the pause)
    exe = exe if state == "READY" else None
    dec = C.decide(ev.get("decision"), state, paper_auto, exe)
    reason = C.setup_reason_text(ev.get("decision"), ev.get("reason_code"), ev.get("reason_text"), raw)
    code = ev.get("reason_code")
    risk = _ticker_risk(exe)
    return {"symbol": ev["symbol"], "evaluated_at": ev["evaluated_at"], "evaluated_at_et": _et(ev["evaluated_at"]), "trigger": ev.get("trigger"), "origin": origin,
            "evaluation_id": ev.get("evaluation_id"), "price": price,
            "setup": {"type": ev.get("setup_type") or SETUP_TYPE, "name": SETUP_NAME, "note": SETUP_NOTE, "state": state, "prior_state": ev.get("prior_state"),
                      "state_label": STATE_LABELS.get(state, state), "evaluation": ev.get("decision"), "reason_code": code, "reason_text": reason,
                      "reason_reworded": reason != (C.REASON_TEXT.get(code or "") or text_for(code)),   # the engine's code would say more than the row shows
                      "setup_id": ev.get("setup_id")},
            "score": x["score"], "values": x["values"], "checks": x["checks"], "passed": x["passed"], "failed": x["failed"], "distance": x["distance"], "risk": risk,
            **dec, "no_trade_reason": _no_trade(dec, reason, x, risk, past), "consistent": x["consistent"], "consistency_note": x["note"], "blockers": x["blockers"]}


def _closest(tickers: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    rows = sorted(tickers, key=lambda t: C.closest_key(t["setup"]["state"], t["distance"]["unmet"], t["distance"]["stretch_gap"], t["distance"]["demand_gap"],
                                                         t["symbol"]))
    return [{"rank": n, "symbol": t["symbol"], "state": t["setup"]["state"], "state_label": t["setup"]["state_label"], "decision": t["decision"],
             "decision_label": t["decision_label"], "unmet": t["distance"]["unmet"], "unmet_root_causes": t["distance"]["unmet_root_causes"],
             "stretch": t["values"]["stretch"], "stretch_gap": t["distance"]["stretch_gap"], "demand": t["values"]["demand"], "demand_gap": t["distance"]["demand_gap"],
             "text": t["distance"]["text"]} for n, t in enumerate(rows, 1)]


def _outcome(at: Optional[str], counts: Dict[str, int], intents: List[str], past: bool = False) -> Dict[str, str]:
    if at is None:
        return {"code": "NO_CYCLE", "text": "No setup evaluation cycle is recorded at or before the requested time." if past else
                "No setup evaluation cycle is recorded yet (the evaluator runs every minute during the regular session)."}
    if intents:
        return {"code": "ORDER_INTENT", "text": f"Paper entry intent recorded: {', '.join(intents)}."}
    if counts[READY]:
        text = (f"No order intent: {counts[READY]} READY ticker(s); no executor result was recorded in the cycle window." if past
                else f"No order intent yet: {counts[READY]} READY ticker(s) awaiting the executor.")
    elif counts[BLOCKED] or counts[SKIPPED]:
        text = f"No trade: {counts[BLOCKED]} READY ticker(s) blocked, {counts[SKIPPED]} skipped."
    elif counts[WATCHING]:
        text = f"No trade: no ticker READY; {counts[WATCHING]} on WATCH."
    else:
        text = "No trade: no ticker reached READY."
    return {"code": "NO_TRADE", "text": text}


def _idle_note(r: Reader, runs: List[Dict[str, Any]], hi: str) -> Optional[str]:
    if runs:
        return None
    s = r.last_before("forward_validation_samples", hi, "at, last_cycle_at, last_cycle_result")
    note = "No executor run is recorded in this window: Monitor-only IDLE cycles are never persisted."
    if s and s.get("last_cycle_at"):
        note += f" The observer saw the last cycle at {_et(s['last_cycle_at']) or s['last_cycle_at']}: {s.get('last_cycle_result') or 'unknown'}."
    return note


def _summary(r: Reader, at: Optional[str], next_at: Optional[str], now: datetime, tickers: List[Dict[str, Any]], closest: List[Dict[str, Any]],
             runs: List[Dict[str, Any]], mode: Dict[str, Any], *, latest: bool, ctrl_at: Optional[str], ref: Optional[str] = None) -> Dict[str, Any]:
    t = at or ref or _iso(now)                                                 # no cycle: the context at the requested time (ref), else now
    sess = classify(_parse(t))
    counts = {d: 0 for d in DECISIONS}
    for x in tickers:
        counts[x["decision"]] += 1
    hi = _window_end(at, next_at) if at else None
    entries = [run["summary"].get("entries") if isinstance(run["summary"].get("entries"), dict) else {} for run in runs]
    submitted = [str(s) for e in entries for s in e.get("submitted") or []]
    xids = [ev.get("execution_id") for e in entries for ev in e.get("evaluations") or [] if isinstance(ev, dict) and ev.get("result") == "ENTRY_INTENT"]
    results = Counter(run["result"] for run in runs)
    return {"cycle_id": cycle_id(at), "at": at, "at_et": _et(at), "next_at": next_at, "next_at_et": _et(next_at),
            "stale": bool(latest and at and _day(at) != _day(_iso(now))), "age_s": int((now - _parse(at)).total_seconds()) if at else None,
            "confirmed": (ctrl_at == at) if latest and at else None,
            "market": {"session": sess["session"], "label": SESSION_LABELS.get(sess["session"], sess["session"]), "calendar": sess["calendar"],
                       "trading_date": sess["trading_date"]},
            "tickers_scanned": len(tickers), "candidate_setups": sum(1 for x in tickers if x["setup"]["state"] in ("WATCH", "READY", "INVALIDATED")),
            "counts": counts, "risk": _risk_at(r, t), "paper_auto": mode,
            "executor": {"runs": len(runs), "results": {"RUN": results.get("RUN", 0), "ERROR": results.get("ERROR", 0)}, "first_at": runs[0]["at"] if runs else None,
                         "last_at": runs[-1]["at"] if runs else None, "first_at_et": _et(runs[0]["at"]) if runs else None,
                         "last_at_et": _et(runs[-1]["at"]) if runs else None, "window": {"from": at, "to": hi},
                         "error_types": sorted({run["error_type"] for run in runs if run.get("error_type")}), "idle_note": _idle_note(r, runs, hi) if at else None},
            "order_intents": len(submitted), "orders_submitted": sum(1 for i in _intents(r, xids) if _ints(i.get("attempt_count")) >= 1),
            "outcome": _outcome(at, counts, submitted, past=not latest),
            "closest": {"symbol": closest[0]["symbol"], "decision": closest[0]["decision"], "unmet": closest[0]["unmet"],
                        "text": f"{closest[0]['symbol']}: {closest[0]['state_label']}; {closest[0]['text']}"} if closest else None}


def _cycle_payload(r: Reader, at: Optional[str], rows: List[Dict[str, Any]], next_at: Optional[str], now: datetime, *, stream: Any = None, latest: bool = False,
                   ctrl_at: Optional[str] = None, ref: Optional[str] = None) -> Dict[str, Any]:
    keep, off = _batch(rows) if at else ([], [])
    mode = _mode_at(r, at or ref or _iso(now))
    runs: List[Dict[str, Any]] = []
    exe: Dict[str, Dict[str, Any]] = {}
    if at:
        hi = _window_end(at, next_at)
        runs = _runs(r, at, hi)
        exe = _merge([(run, _run_results(run)) for run in runs], _captured(r, at, hi) if runs else {})
    tickers = [_ticker_row(x, "SCHEDULER_CYCLE", mode["on"], exe.get(x["symbol"]), _cached_price(stream, x["symbol"], now) if latest else _price_na(PRICE_HISTORY_TEXT),
                           past=not latest) for x in keep]
    closest = _closest(tickers)
    return {"version": VERSION, "generated_at": _iso(now), "broker_calls": 0, "read_only": True,
            "cycle": _summary(r, at, next_at, now, tickers, closest, runs, mode, latest=latest, ctrl_at=ctrl_at, ref=ref), "tickers": tickers, "closest": closest,
            "off_cycle_evaluations": len(off), "legend": legend(), "disclaimer": DISCLAIMER}


# ================================================================ GET /api/decision-flow ====================================================
def latest(now: Optional[datetime] = None, store: Optional[DecisionFlowStore] = None, stream: Any = None) -> Dict[str, Any]:
    """The latest evaluation cycle (confirmed by setup_controls['setup_state_evaluator'].last_run_at when present); when today has none, the most
    recent one with stale=true. Production calls (no argument) are cached for a few seconds, keyed by the newest rows."""
    if now is None and store is None and stream is None:
        st = DecisionFlowStore()
        return _cached(("latest", str(st.path)), LATEST_TTL_S, st, lambda: _latest(datetime.now(UTC), st, _stream_instance()))
    return _latest(now or datetime.now(UTC), store or DecisionFlowStore(), stream if stream is not None else _stream_instance())


def _latest(now: datetime, store: DecisionFlowStore, stream: Any) -> Dict[str, Any]:
    with store.read() as r:
        ctrl_at = (r.one("SELECT last_run_at FROM setup_controls WHERE name = 'setup_state_evaluator'") or {}).get("last_run_at")
        at, rows = None, []
        if ctrl_at:
            rows = _stamp_rows(r, ctrl_at)
            at = ctrl_at if _batch(rows)[0] else None
        if at is None:
            at = _cycle_before(r, None)
            rows = _stamp_rows(r, at) if at else []
        body = _cycle_payload(r, at, rows, None, now, stream=stream, latest=True, ctrl_at=ctrl_at)
    body["universe"] = _universe_status(stream)
    return body


# ================================================================ GET /api/decision-flow/cycle ==============================================
def cycle(at: str, now: Optional[datetime] = None, store: Optional[DecisionFlowStore] = None) -> Dict[str, Any]:
    """One cycle reconstructed: the batch at or before `at`, its executor window [cycle, next cycle) and the context at the cycle time."""
    t = _parse(at)
    if t is None:
        raise ValueError("at must be an ISO 8601 time")
    if not YEARS[0] <= t.year <= YEARS[1]:
        raise ValueError(f"at must be within the years {YEARS[0]}-{YEARS[1]}")
    now = now or datetime.now(UTC)
    with (store or DecisionFlowStore()).read() as r:
        stamp = _cycle_before(r, _iso(t))
        rows = _stamp_rows(r, stamp) if stamp else []
        body = _cycle_payload(r, stamp, rows, _cycle_after(r, stamp) if stamp else None, now, ref=_iso(t))
    body["requested_at"], body["requested_at_et"] = _iso(t), _et(_iso(t))
    return body


# ================================================================ GET /api/decision-flow/history ============================================
def history(date: Optional[str] = None, limit: int = HISTORY_LIMIT, now: Optional[datetime] = None, store: Optional[DecisionFlowStore] = None) -> Dict[str, Any]:
    """One New York trading date: day counts and the cycle list (newest first, at most `limit`)."""
    if date is not None:
        if not _DATE.match(date):
            raise ValueError("date must be YYYY-MM-DD")
        try:
            Date.fromisoformat(date)
        except ValueError:
            raise ValueError("date must be a real calendar date") from None
        if not YEARS[0] <= int(date[:4]) <= YEARS[1]:
            raise ValueError(f"date must be within the years {YEARS[0]}-{YEARS[1]}")
    if not isinstance(limit, int) or not 1 <= limit <= HISTORY_MAX:
        raise ValueError(f"limit must be 1..{HISTORY_MAX}")
    if now is None and store is None:
        st = DecisionFlowStore()
        return _cached(("history", str(st.path), date, limit), HISTORY_TTL_S, st, lambda: _history(date, limit, datetime.now(UTC), st))
    return _history(date, limit, now or datetime.now(UTC), store or DecisionFlowStore())


def _mode_timeline(r: Reader, lo: str, hi: str) -> List[Tuple[str, str]]:
    before = r.last_before("execution_mode_events", lo, "at, to_mode", strict=True)
    events = sorted(r.window("execution_mode_events", lo, hi, "at, to_mode"), key=lambda e: (e["at"], e["rid"]))
    return ([(before["at"], before["to_mode"])] if before else []) + [(e["at"], e["to_mode"]) for e in events]


def _mode_of(timeline: List[Tuple[str, str]], ts: str) -> str:
    i = BS.bisect_right([t for t, _ in timeline], ts)
    return timeline[i - 1][1] if i else "MONITOR_ONLY"


def _spans(timeline: List[Tuple[str, str]], lo: str, hi: str, open_at: Optional[str] = None) -> Tuple[List[Dict[str, Any]], float]:
    """The Paper Auto intervals inside [lo, hi) and their minutes; a last interval still running at open_at (now) ends there with open=true."""
    out: List[Dict[str, Any]] = []
    start: Optional[str] = None
    for at, to in timeline:
        t = max(at, lo)
        if t >= hi:
            break
        if to == "PAPER_AUTO" and start is None:
            start = t
        elif to != "PAPER_AUTO" and start is not None:
            out.append({"from": start, "to": t, "open": False})
            start = None
    if start is not None and start < hi:
        out.append({"from": start, "to": hi, "open": hi == open_at})
    for s in out:
        s["from_et"], s["to_et"] = _et(s["from"]), _et(s["to"])
    return out, round(sum((_parse(s["to"]) - _parse(s["from"])).total_seconds() for s in out) / 60.0, 1)


def _history(date: Optional[str], limit: int, now: datetime, store: DecisionFlowStore) -> Dict[str, Any]:
    with store.read() as r:
        last = r.last_rowid("setup_evaluations")
        newest = r.one("SELECT evaluated_at FROM setup_evaluations WHERE rowid = ?", (last,)) if last is not None else None
        latest_date, today = _day((newest or {}).get("evaluated_at")), _day(_iso(now))
        day = date or (today if _has_rows_on(r, today) else latest_date or today) or now.astimezone(UTC).date().isoformat()
        lo, hi = _day_window(day)
        rows = [x for x in r.window("setup_evaluations", lo, hi, DAY_COLS) if x["trigger"] != "TEST"]   # TEST evaluations are never evidence
        # the day's cycles: SCHEDULED stamps with >= 2 distinct symbols, one row per symbol (the lower rowid)
        stamps: Dict[str, Dict[str, Dict[str, Any]]] = {}
        for x in rows:
            if x["trigger"] == "SCHEDULED":
                stamps.setdefault(x["evaluated_at"], {}).setdefault(x["symbol"], x)
        cycles = sorted(s for s, m in stamps.items() if len(m) >= CYCLE_MIN_SYMBOLS)
        in_cycle = sum(len(stamps[s]) for s in cycles)
        timeline = _mode_timeline(r, lo, hi)
        ready = [x for x in rows if x["resulting_state"] == "READY" and x["decision"] not in ("PAUSED", "ERROR")]
        skipped_off = [x for x in ready if _mode_of(timeline, x["evaluated_at"]) != "PAPER_AUTO"]
        trans = {x["to_state"]: _ints(x["n"]) for x in r.query("SELECT to_state, count(*) AS n FROM setup_transitions WHERE evaluated_at >= ? AND evaluated_at < ? "
                                                             "AND to_state IN ('READY', 'WATCH') AND trigger != 'TEST' GROUP BY to_state", (lo, hi))}
        runs = _runs(r, lo, hi)
        per_run = [(run, _run_results(run)) for run in runs]
        with_candidates = n_intents = 0
        xids: List[Any] = []
        for run, res in per_run:
            e = run["summary"].get("entries") if isinstance(run["summary"].get("entries"), dict) else {}
            with_candidates += 1 if e.get("candidates") else 0
            n_intents += len(e.get("submitted") or [])
            xids += [x.get("execution_id") for x in res.values() if x["result"] == "ENTRY_INTENT"]
        intents = _intents(r, xids)
        gate_cols = "evaluated_at, state, allowed_new_entry"
        gate_rows = sorted(r.window("execution_safety_evaluations", lo, hi, gate_cols), key=lambda x: x["evaluated_at"])
        risk_states = Counter(x["state"] for x in gate_rows)
        prior_gate = r.last_before("execution_safety_evaluations", lo, gate_cols, strict=True)
        spans, minutes = _spans(timeline, lo, min(hi, _iso(now)) if _iso(now) > lo else lo, open_at=_iso(now))
    gate = ([prior_gate] if prior_gate else []) + gate_rows                     # the risk gate in force at each cycle: one read for the whole day
    gate_at = [x["evaluated_at"] for x in gate]
    best = None
    for x in rows:
        if x["decision"] in ("PAUSED", "ERROR"):
            continue
        dist = C.stored_distance(C.loads(x["blockers"], []), x["confidence_status"], x["stretch"], x["demand"])
        key = C.closest_key(x["resulting_state"], dist["unmet"], dist["stretch_gap"], dist["demand_gap"], x["symbol"]) + (x["evaluated_at"],)
        if best is None or key < best[0]:
            best = (key, x, dist)
    closest = None
    if best is not None:
        _, x, dist = best
        label = STATE_LABELS.get(x["resulting_state"], x["resulting_state"])
        unmet = (f"READY held, {dist['unmet']} entry check(s) not met now" if x["resulting_state"] == "READY" and dist["unmet"]
                 else f"{dist['unmet']} READY check(s) unmet")
        closest = {"symbol": x["symbol"], "state": x["resulting_state"], "state_label": label, "unmet": dist["unmet"], "unmet_root_causes": dist["unmet_root_causes"],
                   "stretch": x["stretch"], "stretch_gap": dist["stretch_gap"], "demand": x["demand"], "demand_gap": dist["demand_gap"], "at": x["evaluated_at"],
                   "at_et": _et(x["evaluated_at"]),
                   "text": f"{x['symbol']} at {_et(x['evaluated_at']) or x['evaluated_at']}: {label}; {unmet}"
                           + (f" ({dist['unmet_root_causes']} distinct causes)" if dist["unmet_root_causes"] != dist["unmet"] else "")
                           + (f"; stretch {float(x['stretch']):.1f}" if isinstance(x["stretch"], (int, float)) else "") + "."}
    # every cycle's decisions (the cycle table's own rule): the day's BLOCKED and entry-limit SKIPPED counts are one per ticker per cycle, never one
    # per executor run (a cycle window holds about two runs)
    starts = [run["at"] for run, _ in per_run]
    per_cycle: Dict[str, Tuple[Dict[str, Dict[str, Any]], Dict[str, Dict[str, str]], List[Tuple[Dict[str, Any], Any]], Optional[str]]] = {}
    blocked_by: Counter = Counter()
    blocked_n = skipped_limit = 0
    for idx, at in enumerate(cycles):
        nxt = cycles[idx + 1] if idx + 1 < len(cycles) else None
        w = per_run[BS.bisect_left(starts, at):BS.bisect_left(starts, _window_end(at, nxt))]
        exe = _merge(w, {})
        on = _mode_of(timeline, at) == "PAPER_AUTO"
        decs = {sym: C.decide(x["decision"], x["resulting_state"], on, exe.get(sym) if x["resulting_state"] == "READY" else None) for sym, x in stamps[at].items()}
        for sym, dec in decs.items():
            if dec["decision"] == BLOCKED:
                blocked_n += 1
                blocked_by.update((exe.get(sym) or {}).get("blocked_by") or [])
            elif dec["decision_detail"] == ENTRIES_PER_CYCLE:
                skipped_limit += 1
        per_cycle[at] = (exe, decs, w, nxt)
    items: List[Dict[str, Any]] = []
    for idx in range(len(cycles) - 1, -1, -1):                                  # newest first
        if len(items) >= limit:
            break
        at = cycles[idx]
        exe, decs, w, nxt = per_cycle[at]
        on = _mode_of(timeline, at) == "PAPER_AUTO"
        counts = {d: 0 for d in DECISIONS}
        near = None
        for sym, x in stamps[at].items():
            counts[decs[sym]["decision"]] += 1
            if x["decision"] not in ("PAUSED", "ERROR"):
                dist = C.stored_distance(C.loads(x["blockers"], []), x["confidence_status"], x["stretch"], x["demand"])
                key = C.closest_key(x["resulting_state"], dist["unmet"], dist["stretch_gap"], dist["demand_gap"], sym)
                if near is None or key < near[0]:
                    near = (key, sym, dist, x["stretch"])
        submitted = [str(s) for run, _ in w for s in ((run["summary"].get("entries") or {}).get("submitted") or [])]
        gi = BS.bisect_right(gate_at, at)
        g = gate[gi - 1] if gi else None
        sess = classify(_parse(at))
        mode = "PAPER_AUTO" if on else "MONITOR_ONLY"
        results = Counter(run["result"] for run, _ in w)
        items.append({"cycle_id": cycle_id(at), "at": at, "at_et": _et(at), "next_at": nxt, "next_at_et": _et(nxt), "tickers": len(stamps[at]),
                      "tickers_scanned": len(stamps[at]), "candidate_setups": sum(1 for x in stamps[at].values() if x["resulting_state"] in ("WATCH", "READY", "INVALIDATED")),
                      "counts": counts, "market": {"session": sess["session"], "label": SESSION_LABELS.get(sess["session"], sess["session"]), "calendar": sess["calendar"]},
                      "paper_auto": {"mode": mode, "on": on, "label": MODE_LABELS.get(mode, mode)},
                      "risk": {"state": g["state"], "allowed_new_entry": bool(_ints(g["allowed_new_entry"])), "evaluated_at": g["evaluated_at"]} if g else
                              {"state": None, "allowed_new_entry": None, "evaluated_at": None},
                      "executor": {"runs": len(w), "results": {"RUN": results.get("RUN", 0), "ERROR": results.get("ERROR", 0)}},
                      "executor_runs": len(w), "order_intents": len(submitted), "outcome": _outcome(at, counts, submitted, past=True),
                      "closest": {"symbol": near[1], "unmet": near[2]["unmet"], "stretch": near[3]} if near else None})
    day_block = {"date": day, "timezone": "America/New_York", "latest_date": latest_date, "window": {"from": lo, "to": hi},
                 "cycles": len(cycles), "evaluations": len(rows), "off_cycle_evaluations": len(rows) - in_cycle, "tickers": list(dict.fromkeys(x["symbol"] for x in rows)),
                 "ready_transitions": trans.get("READY", 0), "ready_evaluations": len(ready), "watch_transitions": trans.get("WATCH", 0),
                 "skipped_paper_auto_off": {"count": len(skipped_off), "basis": SKIPPED_BASIS}, "skipped_entry_limit": skipped_limit,
                 "blocked": {"count": blocked_n, "by_control": dict(sorted(blocked_by.items())), "labels": {k: CONTROL_LABELS.get(k, k) for k in sorted(blocked_by)},
                             "basis": BLOCKED_BASIS},
                 "closest": closest, "paper_auto": {"spans": spans, "minutes": minutes}, "executor": {"runs": len(runs), "with_candidates": with_candidates},
                 "order_intents": n_intents, "orders": sum(1 for i in intents if _ints(i.get("attempt_count")) >= 1),
                 "fills": sum(1 for i in intents if _ints(i.get("filled_qty")) > 0), "risk_states": dict(sorted(risk_states.items()))}
    return {"version": VERSION, "generated_at": _iso(now), "broker_calls": 0, "read_only": True, "day": day_block, "cycles": items, "limit": limit,
            "cycles_listed": len(items), "has_more": len(items) < len(cycles), "latest_date": latest_date, "legend": legend(), "disclaimer": DISCLAIMER}


def _has_rows_on(r: Reader, day: Optional[str]) -> bool:
    if not day:
        return False
    lo, hi = _day_window(day)
    row = r.last_before("setup_evaluations", hi, "evaluated_at", strict=True)
    return row is not None and row["evaluated_at"] >= lo


# ================================================================ GET /api/decision-flow/explain ============================================
def explain(symbol: str, date: Optional[str] = None, time: Optional[str] = None, tz: str = "ET", now: Optional[datetime] = None,
            store: Optional[DecisionFlowStore] = None) -> Dict[str, Any]:
    """Why a ticker did (not) trade at a time: its evaluation at or before that time (within 10 minutes), the decision of that evaluation with its
    checks and executor window, and the mode, risk gate, preflight, reconciliation and executor at the asked time."""
    sym = validate_symbol(symbol or "")
    tz = (tz or "ET").strip().upper()
    if tz not in TZ_NAMES:
        raise ValueError("tz must be ET, PT or UTC")
    if not time or not _TIME.match(time):
        raise ValueError("time must be HH:MM or HH:MM:SS")
    zone = _zone(tz)
    if zone is None:
        raise ValueError("no time-zone database here: use tz=UTC")
    now = now or datetime.now(UTC)
    day = date or now.astimezone(zone).date().isoformat()
    if not _DATE.match(day):
        raise ValueError("date must be YYYY-MM-DD")
    if not YEARS[0] <= int(day[:4]) <= YEARS[1]:
        raise ValueError(f"date must be within the years {YEARS[0]}-{YEARS[1]}")
    try:
        y, m, d = (int(p) for p in day.split("-"))
        hms = [int(p) for p in time.split(":")] + [0]
        local = datetime(y, m, d, hms[0], hms[1], hms[2], tzinfo=zone)
    except ValueError:
        raise ValueError("date or time out of range") from None
    t = _iso(local)
    with (store or DecisionFlowStore()).read() as r:
        ev = r.one(f"SELECT rowid AS rid, {EVAL_COLS} FROM setup_evaluations WHERE symbol = ? AND evaluated_at <= ? ORDER BY evaluated_at DESC, rowid DESC LIMIT 1",
                   (sym, t))                                                     # the (symbol, evaluated_at) index: no scan
        age = (local - _parse(ev["evaluated_at"])).total_seconds() if ev else None
        found = ev is not None and age is not None and age <= EXPLAIN_MAX_AGE_S
        mode, risk = _mode_at(r, t), _risk_at(r, t)
        last_run = r.last_before("execution_runs", t, "run_id, started_at, mode, result")
        sample = r.last_before("forward_validation_samples", t, "at, mode, last_cycle_at, last_cycle_result")
        row = None
        if found:
            at = ev["evaluated_at"]
            keep, _ = _batch(_stamp_rows(r, at))
            in_cycle = any(x["symbol"] == sym for x in keep)                   # a symbol written twice in a batch second: still the cycle's ticker
            hi = _window_end(at, _cycle_after(r, at))
            runs = _runs(r, at, hi)
            exe = _merge([(run, _run_results(run)) for run in runs], _captured(r, at, hi) if runs else {})
            row = _ticker_row(ev, _origin(ev.get("trigger"), in_cycle), _mode_at(r, at)["on"], exe.get(sym), _price_na(PRICE_HISTORY_TEXT), past=True)
            row["cycle_id"] = cycle_id(at) if in_cycle else None
    sess = classify(local)
    market = {"session": sess["session"], "label": SESSION_LABELS.get(sess["session"], sess["session"]), "calendar": sess["calendar"], "trading_date": sess["trading_date"]}
    executor = {"last_run": {"at": last_run["started_at"], "at_et": _et(last_run["started_at"]), "mode": last_run.get("mode"), "result": last_run.get("result")}
                if last_run else None,
                "observer": {"sampled_at": sample["at"], "last_cycle_at": sample.get("last_cycle_at"), "last_cycle_at_et": _et(sample.get("last_cycle_at")),
                             "last_cycle_result": sample.get("last_cycle_result")} if sample else None}
    asked = f"{local:%Y-%m-%d %H:%M:%S} {tz}" + ("" if tz == "ET" or not _et(t) else f" ({_et(t)})")
    executor["text"] = _executor_text(executor, t)
    return {"version": VERSION, "generated_at": _iso(now), "broker_calls": 0, "read_only": True,
            "query": {"symbol": sym, "date": day, "time": time, "tz": tz, "at": t, "at_et": _et(t), "max_age_s": EXPLAIN_MAX_AGE_S},
            "asked": {"symbol": sym, "date": day, "time": time, "tz": tz, "utc": t, "et": _et(t)},          # section 6's names for the same question
            "symbol": sym, "found": found, "evaluation": row,
            "nearest_before": None if found or ev is None else {"evaluated_at": ev["evaluated_at"], "evaluated_at_et": _et(ev["evaluated_at"]), "age_s": int(age)},
            "context": {"market": market, "session": market, "mode": mode["mode"], "paper_auto": mode, "risk": risk, "executor": executor},
            "answer": _answer(sym, asked, ev, found, row, mode, risk, market, executor, t), "disclaimer": DISCLAIMER}


def _executor_text(executor: Dict[str, Any], t: str) -> str:
    """What the executor was doing around t, from the persisted run rows or the observer's sample (Monitor-only IDLE cycles are never persisted)."""
    run, obs = executor.get("last_run"), executor.get("observer")
    if run and (_parse(t) - _parse(run["at"])).total_seconds() <= 120:
        return f"The executor's last recorded run started at {run['at_et'] or run['at']} ({run['mode']}, {run['result']})."
    if obs and obs.get("last_cycle_at"):
        return f"The executor's last cycle seen by the observer was at {obs['last_cycle_at_et'] or obs['last_cycle_at']}: {obs.get('last_cycle_result') or 'unknown'}."
    return "No executor cycle is recorded around that time."


def _answer(sym: str, asked: str, ev: Optional[Dict[str, Any]], found: bool, row: Optional[Dict[str, Any]], mode: Dict[str, Any], risk: Dict[str, Any],
            market: Dict[str, Any], executor: Dict[str, Any], t: str) -> str:
    """One plain paragraph from the persisted facts above (no new fact, no estimate)."""
    parts: List[str] = []
    if not found:
        if ev is not None:
            parts.append(f"No evaluation of {sym} is recorded in the 10 minutes up to {asked}; the last one before it was at "
                         f"{_et(ev['evaluated_at']) or ev['evaluated_at']}.")
        else:
            parts.append(f"No evaluation of {sym} is recorded at or before {asked}.")
        parts.append("The setup evaluator runs every minute during the regular session only, so no setup decision applies to that time.")
    else:
        origin = {"SCHEDULER_CYCLE": "a scheduler cycle", "TRADER_VIEW": "a Trader View evaluation", "SUBSCRIBE": "opening the ticker"}.get(row["origin"], row["origin"])
        parts.append(f"At {asked} the last evaluation of {sym} was at {row['evaluated_at_et'] or row['evaluated_at']} ({origin}).")
        parts.append(f"Setup: {row['setup']['state_label']}. {row['setup']['reason_text']}")
        if row["distance"]["unmet"] is not None:
            parts.append(row["distance"]["text"])
    parts.append(("The market was closed" if market["session"] == "CLOSED" else f"Market session: {market['label']}") + ". Paper Auto was "
                 f"{'on' if mode['on'] else 'off (Monitor only)'}; the risk gate was "
                 f"{risk['label']}" + (f" ({', '.join(risk['reason_text'])})" if risk["reason_text"] else "") + ".")
    parts.append(executor.get("text") or _executor_text(executor, t))
    if found:
        # the verdict only: the setup's reason and distance are said above (no_trade_reason repeats them for the per-ticker row)
        verdict = {SETUP_WATCH: "No trade: the setup was on WATCH, not READY.", NO_SETUP: "No trade: no active setup.",
                   INVALIDATED: "No trade: the setup was invalidated.", PAUSED: "No trade: the evaluation was paused.",
                   EVALUATION_ERROR: "No trade: the evaluation failed."}.get(row["decision_detail"])
        parts.append(verdict or row["no_trade_reason"] or "A paper entry intent was recorded for this ticker in that cycle.")
    return " ".join(p.strip() for p in parts if p)


# ================================================================ GET /api/decision-flow/universe ===========================================
def universe(now: Optional[datetime] = None, stream: Any = None) -> Dict[str, Any]:
    """The inactive dynamic-universe plan, or {'available': False, 'text': ...} when it cannot be read. Never changes the live watchlist."""
    now = now or datetime.now(UTC)
    return {"version": VERSION, "generated_at": _iso(now), "broker_calls": 0, "read_only": True,
            "universe": _universe_status(stream if stream is not None else _stream_instance())}
