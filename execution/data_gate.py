"""
execution/data_gate.py - the HDR-2 execution data gate, PURE CORE (spec r14, EXEC-C1a).

`evaluate(facts) -> GateVerdict` takes facts that a collector has already read and returns a verdict
{allowed, order_class, code, codes, failed_facts, provenance} (r14 §3.1). Everything here is pure: no broker,
no store, no network, no clock read, no file I/O. The evaluation clock is a fact (`eval_clock`), injected by the
caller. The collector (`ExecutionService._gate_facts`), the service integration, containment zones, lifecycle
writes and alert surfaces are NOT here (later stages).

Owner decision set applied (EXEC_P1_IMPLEMENTATION_BRIEF §2; only these options are implemented, no switches):
OD-R2-1(b) trigger bound, OD-R2-2(a) E7 refuses before U-3, OD-R4-3(a), OD-R4-5(a), OD-R5-1(a), OD-R5-2(a),
OD-R5-3(a) minute-close age from its stamped start, OD-R6-1(a), OD-R6-2(a) (E10(iv) exists), OD-C-1(a),
OD-C-2(a) (E10(v) relative to S_R), OD-C-5(a) (E-rows only on a real broker).

Codes: where r14 names a code it is used verbatim. Where r14 names no code for a failing row (P1, P2, P5, P6, P7,
P9 at the exit gate, the P8a deferrals, an unreadable P3 read), the code is the row id itself.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Tuple

from execution import POSITION_STATES
from execution.engine import session_policy
from proposals import MINUTE_BAR_MAX_AGE_SECONDS, PRICE_MAX_AGE_SECONDS
from setups.market_hours import session_date, to_et

# ------------------------------------------------------------------ constants (existing values; no new threshold)
FUTURE_TOLERANCE_S = 60.0          # STOP_PRICE_FUTURE_TOLERANCE_S (execution/service.py:61); r14 §2.5
SKEW_MAX_S = 60.0                  # r14 §2.5 skew
BAR_SECONDS = 60.0                 # a minute bar's end = its stamped start + 60 s (r14 §2.3)
PAPER_HOST = "paper-api.alpaca.markets"   # execution/broker.py:31 (duplicated so this module never imports the broker; pinned by a test)
MASK_PLACEHOLDER = "••••"   # paper/alpaca_order_rules.py:208-210: counts as EMPTY for P2
SIMULATED_MODES = ("simulated", "test", "replay")   # proposals/engine.py:29 (pinned by a test)
W_SIMULATED = "SIMULATED_DATA"

REAL, SIMULATED, UNKNOWN = "REAL", "SIMULATED", "UNKNOWN"                       # r14 §2.2 broker classes
COVERED_SESSION, COVERED_CLOSED, UNCOVERED, UNREADABLE_CALENDAR = "COVERED_SESSION", "COVERED_CLOSED", "UNCOVERED", "UNREADABLE"   # r14 §2.6
ENTRY, PROTECTIVE_EXIT, STRATEGY_EXIT = "ENTRY", "PROTECTIVE_EXIT", "STRATEGY_EXIT"   # r14 §2.1
EXIT_GATE, DG_EARLY, DG_FINAL = "EXIT_GATE", "DG_EARLY", "DG_FINAL"
ALLOW, REFUSE, DEFER, HOLD, QTY_DISAGREEMENT, SESSION_FAILURE, NOT_ENTERED = (
    "ALLOW", "REFUSE", "DEFER", "HOLD", "QTY_DISAGREEMENT", "SESSION_FAILURE", "NOT_ENTERED")
NOT_HIT, HIT, BLIND = "NOT_HIT", "HIT", "BLIND"

# r14 §3.3.2: the BLIND reason codes, in r14's listed order (also the primary-code order)
BLIND_REASONS = ("FEED_NOT_REAL", "PRICE_MISSING", "PRICE_INVALID", "PRICE_UNDATED", "PRICE_NAIVE", "PRICE_STALE", "PRICE_FROM_FUTURE",
                 "BAR_NOT_FINAL", "CLOCK_SKEW", "CLOCK_UNREADABLE", "LEVEL_MISSING", "GATE_ERROR")
PHASE1_UNVERIFIABLE_REASONS = ("EVIDENCE_INVALIDATION", "SETUP_INVALIDATED", "DEMAND_FAILURE", "DISLOCATION_NORMALIZED")   # r14 §3.6
SAFETY_KINDS = ("STOP_CHECK_BLIND", "PROTECTIVE_EXIT_UNVERIFIED")             # r14 E10(ii)
UNRESOLVED_STATES = ("ACTIVE", "RECURRENT")
KNOWN_LIFECYCLE_STATES = ("ACTIVE", "RECURRENT", "RESOLVED")

_CAL_CODE = {UNCOVERED: "DG_CALENDAR_UNCOVERED", UNREADABLE_CALENDAR: "DG_CALENDAR_UNREADABLE", COVERED_CLOSED: "DG_CALENDAR_CLOSED"}


class _Unreadable:
    """A fact whose read raised (r14 §3.1: an unreadable fact is a failure, never a default)."""
    def __repr__(self) -> str:
        return "UNREADABLE"

    def __bool__(self) -> bool:
        return False


UNREADABLE = _Unreadable()
_MISSING = object()


def _get(obj: Any, key: str) -> Any:
    if obj is None or obj is UNREADABLE:
        return _MISSING
    if isinstance(obj, Mapping):
        return obj.get(key, _MISSING)
    return getattr(obj, key, _MISSING)


def _readable(obj: Any) -> bool:
    return obj is not None and obj is not UNREADABLE


def _require_aware(dt: Any) -> datetime:
    if not isinstance(dt, datetime) or dt.tzinfo is None or dt.utcoffset() is None:
        raise ValueError("eval_clock must be a timezone-aware datetime")
    return dt


# ------------------------------------------------------------------ timestamps (r14 §2.5 "unverified data")
TS_OK, TS_MISSING, TS_NAIVE, TS_INVALID = "OK", "MISSING", "NAIVE", "INVALID"


def parse_ts(value: Any) -> Tuple[Optional[datetime], str]:
    """A timestamp fact -> (datetime, status). Naive timestamps are NOT assumed UTC (unlike service._parse). `Z` and `+00:00` accepted.
    A number is a POSIX epoch (aware by construction: candle `time`)."""
    if value is None or value is UNREADABLE or value is _MISSING or (isinstance(value, str) and not value.strip()):
        return None, TS_MISSING
    if isinstance(value, bool):
        return None, TS_INVALID
    if isinstance(value, datetime):
        dt = value
    elif isinstance(value, (int, float, Decimal)):
        try:
            f = float(value)
            if not math.isfinite(f):
                return None, TS_INVALID
            dt = datetime.fromtimestamp(f, tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None, TS_INVALID
    elif isinstance(value, str):
        s = value.strip()
        if s[-1:] in ("Z", "z"):
            s = s[:-1] + "+00:00"
        try:
            dt = datetime.fromisoformat(s)
        except ValueError:
            return None, TS_INVALID
    else:
        return None, TS_INVALID
    if dt.tzinfo is None or dt.utcoffset() is None:
        return dt, TS_NAIVE
    return dt, TS_OK


def _num(value: Any) -> Optional[float]:
    """A positive finite number, else None."""
    if value is None or isinstance(value, bool) or value is UNREADABLE or value is _MISSING:
        return None
    try:
        f = float(value)
    except (TypeError, ValueError, ArithmeticError):
        return None
    return f if math.isfinite(f) and f > 0 else None


def _dec(value: Any) -> Optional[Decimal]:
    if value is None or isinstance(value, bool) or value is UNREADABLE or value is _MISSING:
        return None
    try:
        d = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return None
    return d if d.is_finite() else None


def age_basis(source: str, ts: datetime) -> datetime:
    """r14 §2.3 under OD-R5-3(a): a minute close is aged from its STAMPED START; every other source from its source time."""
    return ts


def source_time(source: str, ts: datetime) -> datetime:
    """r14 §2.3: a minute bar's source time is its END (stamped start + 60 s); used for finality and clock ordering only."""
    return ts + timedelta(seconds=BAR_SECONDS) if source == "MINUTE_CLOSE" else ts


def age_limit(source: str) -> int:
    """r14 §2.4 limits (existing constants): a minute close 900 s, a quote / last trade 300 s."""
    return MINUTE_BAR_MAX_AGE_SECONDS if source == "MINUTE_CLOSE" else PRICE_MAX_AGE_SECONDS


# ------------------------------------------------------------------ broker clock (r14 §2.5 "broker clock verified")
@dataclass(frozen=True)
class ClockCheck:
    verified: bool
    problem: Optional[str]          # None | UNREADABLE | UNDATED | NAIVE | SKEW
    timestamp: Optional[datetime]
    skew_s: Optional[float]
    is_open: bool                   # True only when the read said is_open True (meaningful only if verified)


def check_clock(clock: Any, eval_clock: datetime, read_age_s: float = 0.0) -> ClockCheck:
    """Verified = readable, `timestamp` present and timezone-aware, skew <= 60 s. `is_open` is NOT part of verification.
    `read_age_s`: the age of the read on the evaluation clock (r14 §3.3.4, SC's per-cycle read; 0 for a fresh read)."""
    eval_clock = _require_aware(eval_clock)
    if not _readable(clock):
        return ClockCheck(False, "UNREADABLE", None, None, False)
    dt, st = parse_ts(_get(clock, "timestamp"))
    if st in (TS_MISSING, TS_INVALID):
        return ClockCheck(False, "UNDATED", None, None, False)
    if st == TS_NAIVE:
        return ClockCheck(False, "NAIVE", None, None, False)
    age = float(read_age_s)
    if not math.isfinite(age) or age < 0:
        raise ValueError("read_age_s must be a finite non-negative number")
    skew = abs((eval_clock - (dt + timedelta(seconds=age))).total_seconds())
    is_open = _get(clock, "is_open") is True
    if skew > SKEW_MAX_S:
        return ClockCheck(False, "SKEW", dt, skew, is_open)
    return ClockCheck(True, None, dt, skew, is_open)


# ------------------------------------------------------------------ broker class (r14 §2.2)
def broker_class(simulated: Any, environment: Any) -> str:
    """Simulated: `broker.simulated is True`. Real: `simulated is False` AND a fresh environment read with paper True, the paper host, no
    error, not blocked and status ACTIVE. Anything else (a raised read, a missing attribute, ...) is Unknown. Takes no intent, cache or clock."""
    if simulated is True:
        return SIMULATED
    if simulated is not False or not _readable(environment):
        return UNKNOWN
    if (_get(environment, "paper") is True and _get(environment, "host") == PAPER_HOST and _get(environment, "error") is None
            and _get(environment, "blocked") is False and _get(environment, "status") == "ACTIVE"):
        return REAL
    return UNKNOWN


# ------------------------------------------------------------------ calendar (r14 §2.6, §3.6 recount, §3.14)
def classify_calendar(d: date, covered_fn: Callable[[date], Any], market_day_fn: Callable[[date], Any]) -> str:
    """COVERED_SESSION / COVERED_CLOSED / UNCOVERED / UNREADABLE. A raise, or a non-bool answer, is UNREADABLE (never UNCOVERED)."""
    try:
        cov = covered_fn(d)
    except Exception:  # noqa: BLE001 - a failed read is UNREADABLE (r5, N-2)
        return UNREADABLE_CALENDAR
    if not isinstance(cov, bool):
        return UNREADABLE_CALENDAR
    if cov is False:
        return UNCOVERED
    try:
        md = market_day_fn(d)
    except Exception:  # noqa: BLE001
        return UNREADABLE_CALENDAR
    if not isinstance(md, bool):
        return UNREADABLE_CALENDAR
    return COVERED_SESSION if md else COVERED_CLOSED


def _calendar_fact(value: Any) -> str:
    """Anything that is not one of the four facts is UNREADABLE (fail closed)."""
    return value if value in (COVERED_SESSION, COVERED_CLOSED, UNCOVERED, UNREADABLE_CALENDAR) else UNREADABLE_CALENDAR


@dataclass(frozen=True)
class Recount:
    verified: bool
    sessions: Optional[int]
    code: Optional[str]
    codes: Tuple[str, ...] = ()
    dates: Tuple[Tuple[str, str], ...] = ()


def time_exit_recount(opened_at: Any, eval_clock: datetime, covered_fn: Callable[[date], Any], market_day_fn: Callable[[date], Any]) -> Recount:
    """r14 §3.6 TIME_EXIT recount: the dates after `opened_at`'s New York date up to and including today's (sessions_held's endpoints).
    A date counts only if covered AND a market day (a holiday is not counted; an early close counts). Any UNCOVERED date in the window
    makes the recount unverified (DG_CALENDAR_UNCOVERED); any UNREADABLE one too (DG_CALENDAR_UNREADABLE). Never the weekday count."""
    eval_clock = _require_aware(eval_clock)
    opened, st = parse_ts(opened_at)
    if st != TS_OK:
        return Recount(False, None, "DG_UNVERIFIED", ("DG_UNVERIFIED",))
    d0, d1 = session_date(opened), session_date(eval_clock)
    if d0 is None or d1 is None:
        return Recount(False, None, "DG_UNVERIFIED", ("DG_UNVERIFIED",))
    n, d, seen, facts = 0, d0, set(), []
    while d < d1:
        d += timedelta(days=1)
        f = classify_calendar(d, covered_fn, market_day_fn)
        facts.append((d.isoformat(), f))
        seen.add(f)
        if f == COVERED_SESSION:
            n += 1
    codes = tuple(c for c, bad in (("DG_CALENDAR_UNREADABLE", UNREADABLE_CALENDAR in seen), ("DG_CALENDAR_UNCOVERED", UNCOVERED in seen)) if bad)
    if codes:
        return Recount(False, None, codes[0], codes, tuple(facts))
    return Recount(True, n, None, (), tuple(facts))


# ------------------------------------------------------------------ price selection on a real broker (r14 §2.4.1)
@dataclass(frozen=True)
class GatePrice:
    source: str                      # QUOTE | MINUTE_CLOSE | UNAVAILABLE
    value: Any = None
    time: Any = None                 # QUOTE: the trade time; MINUTE_CLOSE: the bar's STAMPED START
    status: str = "UNAVAILABLE"
    final: Optional[bool] = None     # MINUTE_CLOSE only


def select_gate_price(quote: Any, candles: Any, eval_clock: datetime, broker_clock_timestamp: Any) -> GatePrice:
    """`_gate_price`'s pure selection: (1) the quote (status OK, a price, a time) as QUOTE; (2) otherwise the LAST candle that is final BY
    THE CLOCK - `final` set AND end (time + 60 s) <= the evaluation clock AND <= the broker clock's timestamp of the same evaluation -
    as MINUTE_CLOSE; (3) otherwise UNAVAILABLE. Never `finals[-1]` alone."""
    eval_clock = _require_aware(eval_clock)
    if _readable(quote) and _get(quote, "status") == "OK" and _get(quote, "price") not in (None, _MISSING):
        qt = _get(quote, "time")
        if qt is _MISSING:
            qt = _get(quote, "ts")
        if qt not in (None, _MISSING, ""):
            return GatePrice("QUOTE", _get(quote, "price"), qt, "OK", None)
    bt, bst = parse_ts(broker_clock_timestamp)
    if bst == TS_OK and _readable(candles):
        try:
            rows = list(candles)
        except TypeError:
            rows = []
        for c in reversed(rows):
            if _get(c, "final") is not True:
                continue
            start, sst = parse_ts(_get(c, "time"))
            if sst != TS_OK:
                continue
            end = start + timedelta(seconds=BAR_SECONDS)
            if end <= eval_clock and end <= bt:
                return GatePrice("MINUTE_CLOSE", _get(c, "close"), _get(c, "time"), "OK", True)
    return GatePrice("UNAVAILABLE")


# ------------------------------------------------------------------ SC, the stop status (r14 §3.3.2)
@dataclass(frozen=True)
class StopCheckFacts:
    """Real-broker SC facts. No calendar field: SC never reads the calendar (r14 §3.3.2, §3.14)."""
    eval_clock: datetime
    market_mode: Any
    price: Any                       # GatePrice-like (source, value, time, status, final)
    level: Any                       # pos.invalidation_price
    clock: Any                       # SC's per-cycle broker clock read (§3.3.4)
    clock_read_age_s: float = 0.0


@dataclass(frozen=True)
class StopStatus:
    status: str                      # NOT_HIT | HIT | BLIND
    reason: Optional[str]
    codes: Tuple[str, ...] = ()
    provenance: Dict[str, Any] = field(default_factory=dict)


def _ordered(reasons: Iterable[str], order: Tuple[str, ...]) -> Tuple[str, ...]:
    s = set(reasons)
    return tuple(r for r in order if r in s)


def _session_open_dt(eval_clock: datetime) -> datetime:
    et = to_et(eval_clock)
    if et is None:
        raise ValueError("no New York time zone database")
    return et.replace(hour=9, minute=30, second=0, microsecond=0)


def _trigger_facts(f: StopCheckFacts) -> Tuple[List[str], Dict[str, Any], Optional[float], Optional[datetime], ClockCheck]:
    """Every SC check except the level: returns (reasons, provenance, value, basis, clock check)."""
    eval_clock = _require_aware(f.eval_clock)
    reasons: List[str] = []
    prov: Dict[str, Any] = {}
    if not (isinstance(f.market_mode, str) and f.market_mode == "real"):
        reasons.append("FEED_NOT_REAL")
    p = f.price
    source, status = _get(p, "source"), _get(p, "status")
    prov["source"] = source if isinstance(source, str) else None
    if source not in ("QUOTE", "MINUTE_CLOSE") or status != "OK":
        reasons.append("PRICE_MISSING")
    raw_value = _get(p, "value")
    value = None
    if raw_value in (None, _MISSING):
        reasons.append("PRICE_MISSING")
    else:
        value = _num(raw_value)
        if value is None:
            reasons.append("PRICE_INVALID")
    ck = check_clock(f.clock, eval_clock, f.clock_read_age_s)
    prov["clock"] = {"verified": ck.verified, "problem": ck.problem, "skew_s": ck.skew_s, "is_open": ck.is_open}
    if ck.problem == "SKEW":
        reasons.append("CLOCK_SKEW")
    elif ck.problem is not None:
        reasons.append("CLOCK_UNREADABLE")
    basis = None
    ts, tst = parse_ts(_get(p, "time"))
    if tst in (TS_MISSING, TS_INVALID):
        reasons.append("PRICE_UNDATED")
    elif tst == TS_NAIVE:
        reasons.append("PRICE_NAIVE")
    else:
        src = source if source in ("QUOTE", "MINUTE_CLOSE") else "QUOTE"
        basis = age_basis(src, ts)
        stime = source_time(src, ts)
        prov["age_s"] = (eval_clock - basis).total_seconds()
        if (stime - eval_clock).total_seconds() > FUTURE_TOLERANCE_S:
            reasons.append("PRICE_FROM_FUTURE")
        if source == "MINUTE_CLOSE":
            if _get(p, "final") is not True or stime > eval_clock or (ck.timestamp is not None and stime > ck.timestamp):
                reasons.append("BAR_NOT_FINAL")
    return reasons, prov, value, basis, ck


def _within_hit_bound(basis: datetime, eval_clock: datetime, ck: ClockCheck) -> bool:
    """OD-R2-1(b), restated: the source time (a minute close: its stamped start) on or after 09:30 New York time of the session date, with
    SC's broker clock read verified and saying is_open True. No calendar."""
    return ck.verified and ck.is_open and basis >= _session_open_dt(eval_clock)


def stop_status(f: StopCheckFacts) -> StopStatus:
    """r14 §3.3.2 on a real broker. NOT_HIT: every allow-list fact and the age within the §2.4 limit from the age basis, price > level.
    HIT: the allow-list facts, price <= level, within the OD-R2-1(b) bound. BLIND: anything else. An exception is BLIND GATE_ERROR."""
    try:
        reasons, prov, value, basis, ck = _trigger_facts(f)
        level = _num(f.level)
        if level is None:
            reasons.append("LEVEL_MISSING")
        if not reasons:
            src = _get(f.price, "source")
            if value <= level:
                if _within_hit_bound(basis, f.eval_clock, ck):
                    return StopStatus(HIT, None, (), {**prov, "bound": "OD-R2-1(b)"})
                reasons.append("PRICE_STALE")
            elif prov["age_s"] > age_limit(src):
                reasons.append("PRICE_STALE")
            else:
                return StopStatus(NOT_HIT, None, (), prov)
        codes = _ordered(reasons, BLIND_REASONS)
        return StopStatus(BLIND, codes[0], codes, prov)
    except Exception as exc:  # noqa: BLE001 - Z1: never NOT_HIT or HIT on an error
        return StopStatus(BLIND, "GATE_ERROR", ("GATE_ERROR",), {"exception": type(exc).__name__})


def trigger_validity(f: StopCheckFacts) -> Tuple[str, ...]:
    """DG-final's re-check of the trigger's VALIDITY (r14 §3.8): a real source, a timezone-aware source time within its bound from the age
    basis (OD-R2-1(b)), final by the clock. The price LEVEL is not re-tested. Returns the failing reason codes (empty = valid)."""
    reasons, _prov, _value, basis, ck = _trigger_facts(f)
    if not reasons and not _within_hit_bound(basis, f.eval_clock, ck):
        reasons.append("PRICE_STALE")
    return _ordered(reasons, BLIND_REASONS)


# ------------------------------------------------------------------ the verdict
@dataclass(frozen=True)
class GateVerdict:
    allowed: bool
    order_class: str
    code: Optional[str]
    codes: Tuple[str, ...] = ()
    failed_facts: Tuple[str, ...] = ()
    provenance: Dict[str, Any] = field(default_factory=dict)

    @property
    def outcome(self) -> Optional[str]:
        return self.provenance.get("outcome")


class _Fails:
    def __init__(self) -> None:
        self.facts: List[str] = []
        self.codes: List[str] = []

    def add(self, fact: str, *codes: str) -> None:
        if fact not in self.facts:
            self.facts.append(fact)
        for c in codes:
            if c not in self.codes:
                self.codes.append(c)


def _verdict(order_class: str, fails: _Fails, prov: Dict[str, Any], fail_outcome: str) -> GateVerdict:
    if fails.facts:
        prov = {**prov, "outcome": fail_outcome}
        return GateVerdict(False, order_class, fails.codes[0], tuple(fails.codes), tuple(fails.facts), prov)
    return GateVerdict(True, order_class, None, (), (), {**prov, "outcome": ALLOW})


# ------------------------------------------------------------------ ENTRY (r14 §3.2, E0-E10)
@dataclass(frozen=True)
class AttentionBaseline:
    """S_R: this cycle's end-of-RECONCILE open positions {position_id: state}, tagged with the cycle identity (r14 §3.13.7)."""
    cycle_id: Any
    states: Any


@dataclass(frozen=True)
class EntryFacts:
    stage: str                                  # DG_EARLY | DG_FINAL
    eval_clock: datetime
    broker_simulated: Any                       # E0
    environment: Any                            # E0
    market_mode: Any                            # E1
    proposal: Any                               # E2-E4: the proposal RE-READ now (never the intent snapshot)
    expected_proposal_id: Any                   # E2
    proposal_executable: Any                    # E2: the proposal engine's own executability, supplied as a fact
    setup_inputs: Any                           # E6
    broker_clock: Any                           # E4/E5 ordering, E8, E9
    calendar: Any                               # E8
    live_price: Any = None                      # E5 (DG_FINAL only): the gate price of §2.4.1
    stop_check_problems: Any = None             # E10(i)
    protective_exit_problems: Any = None        # E10(i)
    lifecycle_states: Any = None                # E10(ii): iterable of (position_id, kind, state), or UNREADABLE
    latch_keys: Any = None                      # E10(iii)
    attention_baseline: Any = None              # E10(iv)/(v): S_R
    cycle_id: Any = None                        # this cycle's identity
    open_positions_now: Any = None              # E10(iv)/(v): read 2 {position_id: state}, or UNREADABLE
    verified_positions: Any = None              # E10(iv): V, this process's verified set


def _mode_code(mode: Any, other: str) -> Optional[str]:
    if isinstance(mode, str) and mode == "real":
        return None
    return "DG_SIMULATED" if mode in SIMULATED_MODES else other


def _states_map(states: Any) -> Optional[Dict[Any, str]]:
    """A {position_id: state} read, or None if unreadable (a raised read, or any state not in POSITION_STATES)."""
    if not _readable(states) or not isinstance(states, Mapping):
        return None
    out = {}
    for pid, st in states.items():
        if st not in POSITION_STATES:
            return None
        out[pid] = st
    return out


def _price_freshness(fails: _Fails, fact: str, source: Any, raw_ts: Any, eval_clock: datetime, ck: ClockCheck, final: Any) -> None:
    """E4 / E5: present, aware, <= +60 s, within the limit from the age basis; a minute close final by both clocks. An unverified broker
    clock makes every freshness fact of the evaluation unverified (r14 §2.5)."""
    ts, tst = parse_ts(raw_ts)
    if tst != TS_OK:
        fails.add(fact, "DG_UNVERIFIED")
        return
    if not ck.verified:
        fails.add(fact, "DG_UNVERIFIED")
    stime = source_time(source, ts)
    if (stime - eval_clock).total_seconds() > FUTURE_TOLERANCE_S:
        fails.add(fact, "DG_UNVERIFIED")
    if source == "MINUTE_CLOSE" and (final is not True or stime > eval_clock or (ck.timestamp is not None and stime > ck.timestamp)):
        fails.add(fact, "DG_UNVERIFIED")
    if (eval_clock - age_basis(source, ts)).total_seconds() > age_limit(source):
        fails.add(fact, "DG_STALE")


def _evaluate_entry(f: EntryFacts) -> GateVerdict:
    eval_clock = _require_aware(f.eval_clock)
    if f.stage not in (DG_EARLY, DG_FINAL):
        raise ValueError("entry stage must be DG_EARLY or DG_FINAL")
    bc = broker_class(f.broker_simulated, f.environment)
    if bc == SIMULATED:                                         # E0: allow, record nothing (r14 §3.11)
        return GateVerdict(True, ENTRY, None, (), (), {})
    prov: Dict[str, Any] = {"stage": f.stage, "broker_class": bc}
    fails = _Fails()
    if bc == UNKNOWN:                                           # E0: every other row is evaluated only on a real broker
        fails.add("E0", "DG_UNKNOWN")
        return _verdict(ENTRY, fails, prov, REFUSE)
    # E1
    c = _mode_code(f.market_mode, "DG_UNKNOWN")
    if c:
        fails.add("E1", c)
    # E2
    p = f.proposal
    if not _readable(p):
        fails.add("E2", "DG_UNVERIFIED")
    else:
        pid = _get(p, "proposal_id")
        if pid in (None, _MISSING, "") or pid != f.expected_proposal_id:
            fails.add("E2", "DG_UNVERIFIED")
        c = _mode_code(_get(p, "mode"), "DG_UNVERIFIED")
        if c:
            fails.add("E2", c)
        warnings = _get(p, "warnings")
        if warnings is _MISSING or warnings is None:
            warnings = []
        try:
            if any((_get(w, "code") == W_SIMULATED) or w == W_SIMULATED for w in warnings):
                fails.add("E2", "DG_SIMULATED")
        except TypeError:
            fails.add("E2", "DG_UNVERIFIED")
        if f.proposal_executable is not True:
            fails.add("E2", "DG_UNVERIFIED")
    # E3 (OD-R4-5(a): QUOTE_MID is UNVERIFIED before U-11)
    pmode, psrc = _get(p, "price_mode"), _get(p, "price_source")
    if pmode == "DAILY_REFERENCE" or psrc == "DAILY_CLOSE":
        fails.add("E3", "DG_DAILY_FALLBACK")
    elif not (pmode == "INTRADAY" and psrc in ("LAST_TRADE", "MINUTE_CLOSE")):
        fails.add("E3", "DG_UNVERIFIED")
    # clock facts shared by E4, E5, E8, E9
    ck = check_clock(f.broker_clock, eval_clock)
    prov["clock"] = {"verified": ck.verified, "problem": ck.problem, "skew_s": ck.skew_s, "is_open": ck.is_open}
    # E4
    if psrc in ("LAST_TRADE", "MINUTE_CLOSE"):
        _price_freshness(fails, "E4", psrc, _get(p, "price_timestamp"), eval_clock, ck, True)
    else:
        fails.add("E4", "DG_UNVERIFIED")
    # E5 (DG-final only)
    if f.stage == DG_FINAL:
        lp = f.live_price
        lsrc = _get(lp, "source")
        if lsrc not in ("QUOTE", "MINUTE_CLOSE") or _get(lp, "status") != "OK" or _num(_get(lp, "value")) is None:
            fails.add("E5", "DG_UNVERIFIED")
        if lsrc in ("QUOTE", "MINUTE_CLOSE"):
            _price_freshness(fails, "E5", lsrc, _get(lp, "time"), eval_clock, ck, _get(lp, "final"))
    # E6 (stored-time facts)
    si = f.setup_inputs
    if not _readable(si):
        fails.add("E6", "DG_INCOMPLETE")
    else:
        if not (isinstance(_get(si, "mode"), str) and _get(si, "mode") == "real"):
            fails.add("E6", "DG_SIMULATED")
        for key in ("ppe_status", "demand_status", "stretch_status"):
            v = _get(si, key)
            if v == W_SIMULATED:
                fails.add("E6", "DG_SIMULATED")
            elif v != "AVAILABLE":
                fails.add("E6", "DG_INCOMPLETE")
        if _get(si, "quote_status") != "OK":
            fails.add("E6", "DG_INCOMPLETE")
        if any(_get(si, k) is not True for k in ("ppe_fresh", "demand_fresh", "quote_fresh")):
            fails.add("E6", "DG_STALE")
        sr = _get(si, "stale_reasons")
        if sr is _MISSING or sr is None or not isinstance(sr, (list, tuple)) or len(sr) != 0:
            fails.add("E6", "DG_STALE")
        ev, est = parse_ts(_get(si, "evaluated_at"))
        if est != TS_OK or ev > eval_clock:
            fails.add("E6", "DG_INCOMPLETE")
    # E7: before U-3 the setup record carries no source times -> DG_UNVERIFIED always (OD-R2-2(a))
    fails.add("E7", "DG_UNVERIFIED")
    # E8
    cal = _calendar_fact(f.calendar)
    prov["calendar"] = cal
    if cal != COVERED_SESSION:
        fails.add("E8", _CAL_CODE[cal])
    if not (ck.verified and ck.is_open):
        fails.add("E8", "DG_UNVERIFIED")
    # E9
    if not ck.verified:
        fails.add("E9", "DG_UNVERIFIED")
    # E10 (every sub-check evaluated, no short-circuit; (v) listed after (iv))
    _e10(f, fails)
    return _verdict(ENTRY, fails, prov, REFUSE)


def _nonempty(x: Any) -> Optional[bool]:
    """True/False for a collection; None if unreadable."""
    if not _readable(x):
        return None
    try:
        return len(x) > 0
    except TypeError:
        return None


def _e10(f: EntryFacts, fails: _Fails) -> None:
    # (i) this cycle: a BLIND stop with the market open or unknown, or a refused protective exit
    a, b = _nonempty(f.stop_check_problems), _nonempty(f.protective_exit_problems)
    if a is None or b is None:
        fails.add("E10(i)", "DG_SAFETY_STATE_UNREADABLE")
    elif a or b:
        fails.add("E10(i)", "DG_SAFETY_CHECK_FAILED")
    # (ii) durably: STOP_CHECK_BLIND / PROTECTIVE_EXIT_UNVERIFIED ACTIVE or RECURRENT (an unreadable state counts as ACTIVE)
    ls = f.lifecycle_states
    if not _readable(ls):
        fails.add("E10(ii)", "DG_SAFETY_STATE_UNREADABLE")
    else:
        try:
            rows = list(ls)
            blocked = False
            for row in rows:
                _pid, kind, state = row
                if kind in SAFETY_KINDS and (state in UNRESOLVED_STATES or state not in KNOWN_LIFECYCLE_STATES):
                    blocked = True
            if blocked:
                fails.add("E10(ii)", "DG_SAFETY_CHECK_FAILED")
        except (TypeError, ValueError):
            fails.add("E10(ii)", "DG_SAFETY_STATE_UNREADABLE")
    # (iii) the unsaved-blocking-alert latch
    lk = _nonempty(f.latch_keys)
    if lk is None or lk:
        fails.add("E10(iii)", "DG_SAFETY_ALERT_UNSAVED")
    # S_R (this cycle's only) and read 2
    sr = f.attention_baseline
    sr_states = None
    if isinstance(sr, AttentionBaseline) and f.cycle_id is not None and sr.cycle_id == f.cycle_id:
        sr_states = _states_map(sr.states)
    now_states = _states_map(f.open_positions_now)
    # (iv) OD-R6-2(a): every open position not in ATTENTION (in BOTH S_R and read 2) is protective-verified in this process
    if sr_states is None or now_states is None:
        fails.add("E10(iv)", "DG_SAFETY_STATE_UNREADABLE")
    else:
        verified = f.verified_positions
        if not _readable(verified):
            verified = ()
        for pid, st in now_states.items():
            left_out = st == "ATTENTION" and sr_states.get(pid) == "ATTENTION"
            if not left_out and pid not in verified:
                fails.add("E10(iv)", "DG_SAFETY_UNVERIFIED_SINCE_START")
                break
    # (v) OD-C-1(a)/OD-C-2(a): N_t = A_t \ S_R^ATT, per position id
    if sr_states is None or now_states is None:
        fails.add("E10(v)", "DG_SAFETY_STATE_UNREADABLE")
    else:
        a_t = {pid for pid, st in now_states.items() if st == "ATTENTION"}
        s_att = {pid for pid, st in sr_states.items() if st == "ATTENTION"}
        if a_t - s_att:
            fails.add("E10(v)", "DG_ATTENTION_ENTERED_THIS_CYCLE")


# ------------------------------------------------------------------ EXITS (r14 §3.5 P1-P9, §3.6, X8)
@dataclass(frozen=True)
class ExitFacts:
    order_class: str                            # PROTECTIVE_EXIT | STRATEGY_EXIT
    stage: str                                  # EXIT_GATE | DG_FINAL
    eval_clock: datetime
    broker_simulated: Any                       # P1
    environment: Any                            # P1, P2 (fresh)
    entry_identity: Any                         # P2: the entry intent's durable environment (via pos.execution_id); None = link missing
    broker_position: Any                        # P3/P4: fresh position read; None = flat
    order_qty: Any                              # P5
    pos_quantity: Any                           # P5, §3.5.2
    open_orders: Any                            # P6: fresh open orders for the symbol
    broker_clock: Any                           # P8b (fresh)
    near_close_minutes: int                     # P8a-1/P8a-2
    calendar: Any                               # P8c (protective) / X8 (strategy)
    stop: Any = None                            # P9 at the exit gate: this cycle's StopStatus
    trigger: Any = None                         # P9 at DG-final (protective): StopCheckFacts re-collected
    reason: Optional[str] = None                # STRATEGY_EXIT: the candidate reason
    recount: Any = None                         # STRATEGY_EXIT TIME_EXIT: a Recount
    max_hold_sessions: Any = None


def _p_masked(v: Any) -> Optional[str]:
    if not isinstance(v, str) or not v.strip() or v.strip() == MASK_PLACEHOLDER:
        return None
    return v.strip()


def _p1_to_p7(f: ExitFacts, bc: str, fails: _Fails) -> bool:
    """P1-P7, every failure listed. Returns True when §3.5.2's exact condition holds (b is None or int(qty) < pos.quantity)."""
    if bc != REAL:
        fails.add("P1", "P1")
    env, ent = f.environment, f.entry_identity
    mine, theirs = _p_masked(_get(env, "account_number_masked")), _p_masked(_get(ent, "account_number_masked"))
    if (not _readable(ent) or mine is None or theirs is None or mine != theirs or _get(env, "host") != _get(ent, "host")
            or _get(env, "label") != _get(ent, "label") or _get(env, "host") is _MISSING or _get(env, "label") is _MISSING):
        fails.add("P2", "P2")
    oq = _dec(f.order_qty)
    order_ok = oq is not None and oq == oq.to_integral_value() and oq >= 1
    pq = _dec(f.pos_quantity)
    p5_fails = not order_ok or pq is None or oq != pq
    p7_fails = True
    qty_disagreement = False
    b = f.broker_position
    if b is UNREADABLE:
        fails.add("P3", "P3")
        fails.add("P4", "P4")
    elif b is None:
        qty_disagreement = True
        fails.add("P3", "P3_FLAT")
        fails.add("P4", "P4_AVAILABLE")
    else:
        bq = _dec(_get(b, "qty"))
        if bq is None:
            fails.add("P3", "P3")
        else:
            if pq is not None and int(bq) < pq:
                qty_disagreement = True
            if bq <= 0:
                fails.add("P3", "P3_FLAT")
            elif oq is None or bq < oq:
                fails.add("P3", "P3_QTY")
        if _get(b, "side") != "long":
            fails.add("P3", "P3_SIDE")
        qa = _dec(_get(b, "qty_available"))
        if qa is None:
            fails.add("P4", "P4_UNPARSABLE")
        elif oq is None or qa < oq:
            fails.add("P4", "P4_AVAILABLE")
        p7_fails = not (bq is not None and oq is not None and _get(b, "side") == "long" and bq - oq >= 0)
    if p5_fails:
        fails.add("P5", "P5")
    oo = f.open_orders
    if not _readable(oo) or not isinstance(oo, (list, tuple)) or len(oo) > 0:
        fails.add("P6", "P6")
    if p7_fails:
        fails.add("P7", "P7")
    return qty_disagreement


def _evaluate_exit(f: ExitFacts) -> GateVerdict:
    eval_clock = _require_aware(f.eval_clock)
    oc = f.order_class
    if f.stage not in (EXIT_GATE, DG_FINAL):
        raise ValueError("exit stage must be EXIT_GATE or DG_FINAL")
    bc = broker_class(f.broker_simulated, f.environment)
    if bc == SIMULATED:                                         # today's behaviour; record nothing (r14 §3.11)
        return GateVerdict(True, oc, None, (), (), {})
    cal = _calendar_fact(f.calendar)
    prov: Dict[str, Any] = {"stage": f.stage, "broker_class": bc, "calendar": cal}
    fail_outcome = REFUSE if oc == PROTECTIVE_EXIT else HOLD
    fails = _Fails()
    # P9 at the exit gate: the protective path is entered only on HIT
    if oc == PROTECTIVE_EXIT and f.stage == EXIT_GATE and _get(f.stop, "status") != HIT:
        fails.add("P9", "P9")
        return _verdict(oc, fails, prov, NOT_ENTERED)
    # P8a-1: local session policy, no broker read
    sp1 = session_policy(eval_clock, None, int(f.near_close_minutes))
    if sp1.result != "PASS":
        prov["session_reason"] = sp1.reason
        prov["step"] = "P8a-1"
        if f.stage == DG_FINAL:
            fails.add("P8a-1", "DG_FINAL_SESSION_FAILED")
            return _verdict(oc, fails, prov, SESSION_FAILURE)
        fails.add("P8a-1", "P8a-1")
        return _verdict(oc, fails, prov, DEFER)
    # P8b: broker clock verified (is_open is not part of it) -> refusal
    ck = check_clock(f.broker_clock, eval_clock)
    prov["clock"] = {"verified": ck.verified, "problem": ck.problem, "skew_s": ck.skew_s, "is_open": ck.is_open}
    if not ck.verified:
        fails.add("P8b", "P8_CLOCK_" + ck.problem)
        return _verdict(oc, fails, prov, fail_outcome)
    # P8a-2: evaluated only on a verified clock; OD-R5-1(a) -> deferral at the exit gate; a DG-final session failure at DG-final
    sp2 = session_policy(eval_clock, {"is_open": ck.is_open}, int(f.near_close_minutes))
    if sp2.result != "PASS":
        prov["session_reason"] = sp2.reason
        prov["step"] = "P8a-2"
        if f.stage == DG_FINAL:
            fails.add("P8a-2", "DG_FINAL_SESSION_FAILED")
            return _verdict(oc, fails, prov, SESSION_FAILURE)
        fails.add("P8a-2", "P8a-2")
        return _verdict(oc, fails, prov, DEFER)
    # P1-P7 (every failure listed), then P8c / X8 and the reason row
    qty_disagreement = _p1_to_p7(f, bc, fails)
    if oc == PROTECTIVE_EXIT:
        if cal == COVERED_CLOSED:                               # OD-R4-3(a)
            fails.add("P8c", "P8_SESSION_SOURCES_DISAGREE")
        elif cal == UNREADABLE_CALENDAR:                        # OD-R5-2(a)
            fails.add("P8c", "P8_CALENDAR_UNREADABLE")
        # COVERED_SESSION: holds; UNCOVERED: not applied (P8b + P8a-2 are the session-eligibility fact; provenance calendar UNCOVERED)
        if f.stage == DG_FINAL:
            tf = f.trigger
            bad = trigger_validity(tf) if isinstance(tf, StopCheckFacts) else ("PRICE_MISSING",)
            if bad:
                fails.add("P9", *bad)
    else:
        if cal != COVERED_SESSION:                              # X8 (= E8): COVERED_SESSION and the broker session fact
            fails.add("X8", _CAL_CODE[cal])
        if not (ck.verified and ck.is_open):
            fails.add("X8", "DG_UNVERIFIED")
        _strategy_reason(f, fails)
    outcome = QTY_DISAGREEMENT if qty_disagreement else fail_outcome
    return _verdict(oc, fails, prov, outcome)


def _strategy_reason(f: ExitFacts, fails: _Fails) -> None:
    """r14 §3.6, Phase 1 column: only TIME_EXIT can verify (the recount >= max_hold_sessions); the four others are DG_UNVERIFIED.
    HARD_INVALIDATION never verifies as a strategy candidate (§3.4); an unknown reason is unverified."""
    r = f.reason
    if r == "TIME_EXIT":
        rc = f.recount
        mx = f.max_hold_sessions
        if not isinstance(rc, Recount) or not rc.verified:
            codes = rc.codes if isinstance(rc, Recount) and rc.codes else ("DG_UNVERIFIED",)
            fails.add("REASON", *codes)
        elif not isinstance(mx, int) or isinstance(mx, bool) or not isinstance(rc.sessions, int) or rc.sessions < mx:
            fails.add("REASON", "DG_UNVERIFIED")
    else:
        fails.add("REASON", "DG_UNVERIFIED")


# ------------------------------------------------------------------ C1c1 additions: pure helpers the exit collector needs (no C1a function changed)
EXIT_RESERVE_READS = 8                                       # r14 §3.8.1: exit gate (P1, P3/P4, P6, P8b) + DG-final (the same four, fresh)
NEVER_ATTEMPTED_FAILURES = ("DG_GATE_ERROR", "STALE_PENDING")   # r14 §3.8.2: the only two ways an intent is left behind before the EXIT_INTENT update
COLLISION_NONE, COLLISION_OPEN, COLLISION_NEVER_ATTEMPTED, COLLISION_ATTEMPTED = "NONE", "OPEN", "NEVER_ATTEMPTED", "ATTEMPTED"


def reads_remaining(budget: Any, reads: Any, reads0: Any) -> int:
    """r14 §3.8.1: remaining = budget - (reads - reads0), from the broker's PUBLIC `budget` and `reads` and the service's snapshot `reads0` taken
    immediately after its reset_budget(). A missing or non-integer value (or no snapshot) means 0 (fail closed: the evaluation defers). Never
    reads `_transport` or `left`. The estimate never overstates: every read since the snapshot is subtracted."""
    if any(type(v) is not int for v in (budget, reads, reads0)):
        return 0
    return max(0, budget - (reads - reads0))


def classify_collision(found: Any, position_id: Any, next_cycle: Any, open_states: Iterable[str]) -> str:
    """r14 §3.8.2: what the store read of exit_id_for(position_id, exit_cycle + 1) found. NONE: no intent. OPEN: an open intent (a race).
    NEVER_ATTEMPTED: the STRICT test - every one of state NOT_SENT, attempt_count 0, no broker_order_id, no submitted_at, no outcome_class,
    intent_type EXIT, the same position_id, exit_cycle == next_cycle and failure_code in {DG_GATE_ERROR, STALE_PENDING}. ATTEMPTED: anything else
    (a missing field fails the strict test: fail closed)."""
    if found is None:
        return COLLISION_NONE
    state = _get(found, "state")
    if state in tuple(open_states):
        return COLLISION_OPEN
    strict = (state == "NOT_SENT" and _get(found, "attempt_count") == 0 and _get(found, "broker_order_id") in (None, "")
              and _get(found, "submitted_at") in (None, "") and _get(found, "outcome_class") in (None, "") and _get(found, "intent_type") == "EXIT"
              and _get(found, "position_id") == position_id and _get(found, "exit_cycle") == next_cycle
              and _get(found, "failure_code") in NEVER_ATTEMPTED_FAILURES)
    return COLLISION_NEVER_ATTEMPTED if strict else COLLISION_ATTEMPTED


# ------------------------------------------------------------------ the entry point
def evaluate(facts: Any) -> GateVerdict:
    """The pure gate. ENTRY: E0-E10. PROTECTIVE_EXIT: P1-P9 in the r14 §3.5.1 order. STRATEGY_EXIT: the §3.6 row, X8 and P1-P8.
    Fail-closed: an exception inside the evaluation never allows (ENTRY code DG_GATE_ERROR; exits GATE_ERROR)."""
    if isinstance(facts, EntryFacts):
        oc = ENTRY
    elif isinstance(facts, ExitFacts) and facts.order_class in (PROTECTIVE_EXIT, STRATEGY_EXIT):
        oc = facts.order_class
    else:
        oc = str(getattr(facts, "order_class", None))
        return GateVerdict(False, oc, "DG_UNKNOWN", ("DG_UNKNOWN",), ("ORDER_CLASS",), {"outcome": REFUSE})
    try:
        return _evaluate_entry(facts) if oc == ENTRY else _evaluate_exit(facts)
    except Exception as exc:  # noqa: BLE001 - an exception never yields allow
        code = "DG_GATE_ERROR" if oc == ENTRY else "GATE_ERROR"
        return GateVerdict(False, oc, code, (code,), ("GATE_ERROR",), {"outcome": REFUSE if oc != STRATEGY_EXIT else HOLD,
                                                                         "exception": type(exc).__name__})
