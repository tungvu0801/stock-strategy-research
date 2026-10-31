"""
evidence_refresh/service.py - one Stage 5.4.2 refresh run, with every dependency injected (tests never touch the network or sleep).

A run: mode -> one run at a time (in-process lock) -> outside the regular session -> evidence network enabled -> the shared
`evidence_refresher` lease -> plan (packets + the attempt journal) -> circuit closed -> for each symbol to build, sequentially: renew
the lease, re-read that symbol's packets and journal and build only if it is STILL due (a manual Build or another process may have just
added one), then the same engine call as POST /api/evidence/packets, built in memory and stored by persist_unless_superseded, which
decides inside the store's write transaction (so a concurrent manual Build can never be duplicated by the refresh).

Session rule (DESIGN_542 section 2, step 3): no run or build starts during the regular session or within PRE_OPEN_MARGIN_S of the open
(default_session_blocked), and the insert itself re-checks the session inside the write transaction: a build that finishes after the open
is discarded, never stored (A_DISCARDED), and the run stops.

Bounded retries: a FAILED packet, or an attempt that raised before any packet was stored (recorded in journal.py, durable), backs that
symbol off for retry_hours, at most max_failures_per_day per symbol per 24 h, and spends the daily build budget. A refused source
(ACCESS_DENIED) or two errors in a row stop the run AND open the circuit for retry_hours (durable): no build at all until it closes.
"""
from __future__ import annotations

import logging
import re
import threading
import uuid
from collections import deque
from datetime import datetime, timedelta, timezone
from typing import Callable, Dict, List, Optional

import config
from evidence_refresh import (A_BUILT, A_DISCARDED, A_ERROR, A_FAILED, A_SKIPPED_FRESH, A_SUPERSEDED, A_WOULD_BUILD, BUILD,
                              CIRCUIT_FETCH_STATUSES, CIRCUIT_STOP_REASONS, DISCLAIMER, FRESH, LEASE_NAME, MAX_CONSECUTIVE_ERRORS, MODES,
                              PRE_OPEN_MARGIN_S, R_CIRCUIT_OPEN, R_COMPLETED, R_DRY_RUN, R_ERROR, R_LEASE_HELD, R_NOTHING_DUE, R_OFF,
                              R_RUN_IN_PROGRESS, R_SKIPPED_NETWORK, R_SKIPPED_SESSION, R_STOPPED, SESSION_OPEN, SUPERSEDED_FRESH,
                              SUPERSEDED_IDENTICAL, VERSION)
from evidence_refresh import policy as P
from evidence_refresh.journal import AttemptJournal

log = logging.getLogger(__name__)
LEASE_TTL_MIN = 15
PACKETS_PER_SYMBOL = 100                     # trader_view.research.PACKETS_READ: the window the gate itself reads
MAX_SYMBOLS = 50
_SYMBOL = re.compile(r"^[A-Z][A-Z.]{0,9}$")   # the Build route's own ticker pattern (upper-cased)


def _utc(d: Optional[datetime] = None) -> datetime:
    d = d or datetime.now(timezone.utc)
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def mode() -> str:
    m = str(getattr(config, "EVIDENCE_REFRESH_MODE", "OFF") or "OFF").strip().upper()
    return m if m in MODES else "OFF"


def settings() -> P.RefreshSettings:
    from setups import EVIDENCE_MAX_AGE_DAYS                     # the READY rule's own limit, never a copy
    # refresh age >= 1 day: a packet built today is never rebuilt today. The store keeps one packet per packet_hash (UNIQUE; the hash covers
    # the request incl. its as_of_date, not the ids or created_at), so a same-day rebuild of unchanged evidence could only collide.
    return P.RefreshSettings(refresh_age_days=max(1, int(config.EVIDENCE_REFRESH_AGE_DAYS)), max_age_days=int(EVIDENCE_MAX_AGE_DAYS),
                             retry_hours=max(0.0, float(config.EVIDENCE_REFRESH_RETRY_HOURS)),
                             max_failures_per_day=max(1, int(config.EVIDENCE_REFRESH_MAX_FAILURES_PER_DAY)),
                             max_builds_per_run=max(0, int(config.EVIDENCE_REFRESH_MAX_BUILDS_PER_RUN)),
                             max_builds_per_day=max(0, int(config.EVIDENCE_REFRESH_MAX_BUILDS_PER_DAY)))


def default_symbols() -> List[str]:
    """The watchlist the setup evaluator watches (scanner.watchlist, the same file Trader View reads), validated, in order."""
    from scanner.watchlist import load_watchlist
    out: List[str] = []
    for s in load_watchlist():
        u = str(s).strip().upper()
        if _SYMBOL.match(u) and u not in out:
            out.append(u)
    return out[:MAX_SYMBOLS]


def default_rows(symbol: str) -> List[dict]:
    from evidence.store import EvidenceStore
    st = EvidenceStore()
    if not st.exists():
        return []
    return st.packets(limit=PACKETS_PER_SYMBOL, ticker=symbol)


def _default_store():
    from evidence.store import EvidenceStore
    return EvidenceStore()


def _wall() -> datetime:
    return datetime.now(timezone.utc)


def persist_unless_superseded(store, built: dict, symbol: str, s: P.RefreshSettings, now: datetime,
                              in_session: Optional[Callable[[datetime], bool]] = None) -> dict:
    """Store one refresh build unless, at this moment, it is no longer wanted - decided INSIDE the store's own write transaction.

    EvidenceStore.write() opens BEGIN IMMEDIATE, so every other writer (a manual Build's insert included, in any process) waits until this
    commits: nothing can land between the check and the insert. The build is not stored when the regular session is open at `now`
    (in_session; default: the regular-session calendar), when the symbol already has a FRESH gate packet (someone built one while this
    build was fetching) or when the identical packet (same packet_hash, UNIQUE) is already stored. Only INSERTs, through the store's own
    column lists and encoder - the rows EvidenceStore.insert_packet writes; no existing row is touched."""
    from evidence.store import FETCH_COLS, ITEM_COLS, PACKET_COLS
    packet, items, fetches = built["packet"], built["items"], built["fetches"]
    sym = symbol.upper()
    session_open = in_session or default_regular_session
    with store.write() as c:
        if session_open(now):                              # evidence never changes under an open session (checked under the write lock)
            return {"written": False, "reason": SESSION_OPEN, "existing_packet_id": None}
        rows = [dict(r) for r in c.execute("SELECT packet_id, ticker, status, created_at, failure_code, strength FROM evidence_packets "
                                           "WHERE ticker = ? ORDER BY created_at DESC, packet_id LIMIT ?", (sym, PACKETS_PER_SYMBOL))]
        st = P.symbol_state(sym, rows, now, s)
        if st["state"] == FRESH:
            return {"written": False, "reason": SUPERSEDED_FRESH, "existing_packet_id": st["packet_id"]}
        same = c.execute("SELECT packet_id FROM evidence_packets WHERE packet_hash = ?", (packet["packet_hash"],)).fetchone()
        if same is not None:
            return {"written": False, "reason": SUPERSEDED_IDENTICAL, "existing_packet_id": same[0]}
        enc = store._encode
        c.execute(f"INSERT INTO evidence_packets ({', '.join(PACKET_COLS)}) VALUES ({', '.join('?' * len(PACKET_COLS))})", enc(packet, PACKET_COLS))
        for f in fetches:
            c.execute(f"INSERT INTO evidence_source_fetches ({', '.join(FETCH_COLS)}) VALUES ({', '.join('?' * len(FETCH_COLS))})", enc(f, FETCH_COLS))
        for it in items:
            c.execute(f"INSERT INTO evidence_items ({', '.join(ITEM_COLS)}) VALUES ({', '.join('?' * len(ITEM_COLS))})", enc(it, ITEM_COLS))
    return {"written": True, "reason": None, "existing_packet_id": None}


def default_build(symbol: str, now: datetime, *, adapters: Optional[dict] = None, now_fn: Optional[Callable[[], datetime]] = None,
                  store=None, settings_fn: Optional[Callable[[], P.RefreshSettings]] = None,
                  in_session: Optional[Callable[[datetime], bool]] = None) -> dict:
    """The engine call of POST /api/evidence/packets with only a ticker (default sources, lookback and adapters). The one difference from
    the Build button: the packet is built in memory (persist=False) and stored by persist_unless_superseded, so a packet someone stored
    while this build was fetching is never duplicated. Any exception propagates: the refresher records it in the attempt journal."""
    from evidence import engine as EN
    clock = now_fn or _wall
    st = store if store is not None else _default_store()
    out = EN.build_packet(EN.EvidenceRequest(ticker=symbol), adapters=adapters, now_fn=clock, store=st, persist=False)
    p = out["packet"]
    w = persist_unless_superseded(st, out, symbol, (settings_fn or settings)(), _utc(clock()), in_session=in_session)
    refused = sorted({f.get("host") for f in out.get("fetches", []) if f.get("status") in CIRCUIT_FETCH_STATUSES and f.get("host")})
    return {"status": p["status"], "packet_id": p["packet_id"] if w["written"] else None, "built_packet_id": p["packet_id"],
            "written": w["written"], "superseded": w["reason"], "existing_packet_id": w["existing_packet_id"], "failure_code": p.get("failure_code"),
            "strength": p.get("strength"), "n_items": p.get("n_items"), "n_failed_fetches": p.get("n_failed_fetches"), "refused_hosts": refused,
            "model_requests": 0}


def default_regular_session(now: datetime) -> bool:
    """The regular session itself (the evaluator's calendar): the persistence check."""
    from setups.market_hours import is_regular_session
    return bool(is_regular_session(now))


def default_session_blocked(now: datetime) -> bool:
    """The start gate: the regular session, or the open is less than PRE_OPEN_MARGIN_S away (setups.market_hours.next_session_open treats
    every weekday as a session, so on a holiday the margin is merely conservative)."""
    from setups.market_hours import next_session_open
    if default_regular_session(now):
        return True
    nxt = next_session_open(now)
    return nxt is not None and (nxt - _utc(now)).total_seconds() < PRE_OPEN_MARGIN_S


def _lease_store():
    from forward.automation import default_store
    return default_store()


class EvidenceRefresher:
    def __init__(self, *, mode_fn: Callable[[], str] = mode, settings_fn: Callable[[], P.RefreshSettings] = settings,
                 symbols_fn: Callable[[], List[str]] = default_symbols, rows_fn: Callable[[str], List[dict]] = default_rows,
                 build_fn: Callable[[str, datetime], dict] = default_build, regular_session_fn: Callable[[datetime], bool] = default_session_blocked,
                 network_fn: Callable[[], bool] = lambda: bool(config.EVIDENCE_NETWORK_ENABLED), lease_store_fn: Optional[Callable[[], object]] = _lease_store,
                 clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc), sleep_fn: Optional[Callable[[float], bool]] = None,
                 spacing_s: Optional[float] = None, owner: Optional[str] = None, journal: Optional[AttemptJournal] = None):
        self.mode_fn, self.settings_fn, self.symbols_fn, self.rows_fn, self.build_fn = mode_fn, settings_fn, symbols_fn, rows_fn, build_fn
        self.regular_session_fn, self.network_fn, self.lease_store_fn, self.clock = regular_session_fn, network_fn, lease_store_fn, clock
        self.journal = journal if journal is not None else AttemptJournal()      # durable: runtime/evidence_refresh_journal.json
        self._stop = threading.Event()
        self.sleep_fn = sleep_fn or (lambda s: self._stop.wait(s))     # True = stop requested while waiting
        self.spacing_s = float(spacing_s if spacing_s is not None else config.EVIDENCE_REFRESH_SPACING_SECONDS)
        self.owner = owner or f"evidence-refresh-{uuid.uuid4().hex[:12]}"
        self._lock = threading.Lock()
        self.history: deque = deque(maxlen=60)
        self.runs = 0
        self.last_run_at: Optional[str] = None
        self.last_result: Optional[dict] = None
        self.last_error: Optional[str] = None
        self.totals = {A_BUILT: 0, A_FAILED: 0, A_ERROR: 0, A_SKIPPED_FRESH: 0, A_SUPERSEDED: 0, A_DISCARDED: 0, A_WOULD_BUILD: 0}

    # ------------------------------------------------------------------------------------------------ read-only
    def plan(self, now: Optional[datetime] = None) -> dict:
        now = _utc(now or self.clock())
        symbols = list(self.symbols_fn())
        rows: Dict[str, List[dict]] = {}
        errors: List[str] = []
        unreadable: List[str] = []
        for s in symbols:
            try:
                rows[s] = list(self.rows_fn(s))
            except Exception as exc:  # noqa: BLE001 - an unreadable store never crashes, and never reads as MISSING (that would build)
                rows[s] = []
                unreadable.append(s)
                errors.append(f"{s}: {type(exc).__name__}")
        js = self.journal.state(now)
        by_symbol: Dict[str, List[dict]] = {}
        for e in js["errors"]:
            by_symbol.setdefault(e["symbol"].upper(), []).append(e)
        out = P.plan(symbols, rows, now, self.settings_fn(), errors_by_symbol=by_symbol, circuit=js["circuit"], unreadable=unreadable)
        out["read_errors"] = errors
        jp = self.journal.path()
        out["journal"] = {"path": str(jp) if jp else None, "errors_kept": len(js["errors"]), "read_error": self.journal.read_error,
                          "write_error": self.journal.write_error}
        return out

    def request_stop(self) -> None:
        self._stop.set()

    # ------------------------------------------------------------------------------------------------ one run
    def _record(self, at: datetime, symbol: str, outcome: str, **kw) -> dict:
        row = {"at": _utc(at).isoformat(timespec="seconds"), "symbol": symbol, "outcome": outcome, **kw}
        self.history.appendleft(row)
        self.totals[outcome] = self.totals.get(outcome, 0) + 1
        return row

    def _finish(self, now: datetime, result: dict) -> dict:
        self.runs += 1
        self.last_run_at = _utc(now).isoformat(timespec="seconds")
        self.last_result = result
        return result

    def run_once(self, now: Optional[datetime] = None) -> dict:
        now = _utc(now or self.clock())
        m = self.mode_fn()
        if m == "OFF":
            return {"result": R_OFF}
        if not self._lock.acquire(blocking=False):
            return {"result": R_RUN_IN_PROGRESS}
        lease = None
        try:
            if self.regular_session_fn(now):
                return self._finish(now, {"result": R_SKIPPED_SESSION, "mode": m})
            if not self.network_fn():
                return self._finish(now, {"result": R_SKIPPED_NETWORK, "mode": m})
            if self.lease_store_fn is not None:
                lease = self.lease_store_fn()
                if not lease.acquire(self.owner, now, ttl_min=LEASE_TTL_MIN, name=LEASE_NAME):
                    lease = None
                    return self._finish(now, {"result": R_LEASE_HELD, "mode": m})
            p = self.plan(now)
            if p.get("circuit") and p["circuit"].get("open"):
                return self._finish(now, {"result": R_CIRCUIT_OPEN, "mode": m, "until": p["circuit"]["until"], "reason": p["circuit"]["reason"]})
            if not p["to_build"]:
                return self._finish(now, {"result": R_NOTHING_DUE, "mode": m, "wanted": p["wanted"], "budget": p["budget"]})
            if m == "DRY_RUN":
                for s in p["to_build"]:
                    self._record(now, s, A_WOULD_BUILD)
                log.info("evidence refresh (dry run): would build %s", ", ".join(p["to_build"]))
                return self._finish(now, {"result": R_DRY_RUN, "mode": m, "would_build": list(p["to_build"])})
            return self._finish(now, self._build_all(p["to_build"], lease, m))
        except Exception as exc:  # noqa: BLE001 - a refresh run never crashes the server; the next run tries again
            self.last_error = type(exc).__name__
            log.warning("evidence refresh run failed: %s", type(exc).__name__)
            return self._finish(now, {"result": R_ERROR, "error": type(exc).__name__})
        finally:
            if lease is not None:
                try:
                    lease.release(self.owner, name=LEASE_NAME)
                except Exception:  # noqa: BLE001 - the lease expires by itself
                    pass
            self._lock.release()

    def _build_all(self, symbols: List[str], lease, m: str) -> dict:
        attempts: List[dict] = []
        stop_reason = None
        errors_in_row = 0
        s = self.settings_fn()
        for i, sym in enumerate(symbols):
            if i and self.spacing_s > 0 and self.sleep_fn(self.spacing_s):
                stop_reason = "SHUTDOWN"
                break
            if self._stop.is_set():
                stop_reason = "SHUTDOWN"
                break
            t = _utc(self.clock())
            if self.regular_session_fn(t):
                stop_reason = "SESSION_OPENED"
                break
            if lease is not None and not lease.acquire(self.owner, t, ttl_min=LEASE_TTL_MIN, name=LEASE_NAME):
                stop_reason = "LEASE_LOST"
                break
            try:                                                  # duplicate prevention: is it STILL due right now (packets AND journal)?
                again = P.symbol_state(sym, self.rows_fn(sym), t, s, self.journal.errors_by_symbol(t).get(sym.upper(), []))
            except Exception as exc:  # noqa: BLE001 - cannot tell whether it is still due: do not build it this run
                attempts.append(self._record(t, sym, A_ERROR, error=f"recheck {type(exc).__name__}"))
                continue
            if again["decision"] != BUILD:
                attempts.append(self._record(t, sym, A_SKIPPED_FRESH, state=again["state"], packet_id=again["packet_id"]))
                continue
            try:
                out = self.build_fn(sym, t)
            except Exception as exc:  # noqa: BLE001 - one symbol's error never stops the others (until two in a row)
                errors_in_row += 1
                self.last_error = type(exc).__name__
                durable = self.journal.record_error(sym, t, type(exc).__name__)   # no packet was stored: the journal bounds the retries
                attempts.append(self._record(t, sym, A_ERROR, error=type(exc).__name__, journaled=durable))
                log.warning("evidence refresh: %s build raised %s (retry after the back-off)", sym, type(exc).__name__)
                if errors_in_row >= MAX_CONSECUTIVE_ERRORS:
                    stop_reason = "CONSECUTIVE_ERRORS"
                    break
                continue
            errors_in_row = 0
            if out.get("written") is False and out.get("superseded") == SESSION_OPEN:   # finished after the open: discarded, not a failure
                attempts.append(self._record(t, sym, A_DISCARDED, reason=SESSION_OPEN, built_status=out.get("status")))
                log.info("evidence refresh: %s built but not stored (the regular session opened); rebuilt after the close", sym)
                stop_reason = "SESSION_OPENED"
            elif out.get("written") is False:                     # built, but someone stored a packet first: nothing written
                attempts.append(self._record(t, sym, A_SUPERSEDED, reason=out.get("superseded"), packet_id=out.get("existing_packet_id"),
                                             built_status=out.get("status")))
                if out.get("superseded") == SUPERSEDED_IDENTICAL:  # the gate may still want a build: bounded like an error
                    self.journal.record_error(sym, t, SUPERSEDED_IDENTICAL, stage="insert")
                log.info("evidence refresh: %s not stored (%s; packet %s)", sym, out.get("superseded"), out.get("existing_packet_id"))
            else:
                ok = out.get("status") == "COMPLETED"
                attempts.append(self._record(t, sym, A_BUILT if ok else A_FAILED, packet_id=out.get("packet_id"), failure_code=out.get("failure_code"),
                                             strength=out.get("strength"), refused_hosts=out.get("refused_hosts") or []))
                log.info("evidence refresh: %s %s packet %s%s", sym, "built" if ok else "FAILED", out.get("packet_id"),
                         f" ({out.get('failure_code')})" if out.get("failure_code") else "")
            if out.get("refused_hosts"):
                stop_reason = "SOURCE_REFUSED"
                log.warning("evidence refresh: %s refused the requests; stopping this run", ", ".join(out["refused_hosts"]))
                break
            if stop_reason == "SESSION_OPENED":
                break
        circuit = None
        if stop_reason in CIRCUIT_STOP_REASONS and s.retry_hours > 0:
            t = _utc(self.clock())
            until = t + timedelta(hours=s.retry_hours)
            self.journal.open_circuit(t, until, stop_reason)
            circuit = {"until": until.isoformat(timespec="seconds"), "reason": stop_reason}
            log.warning("evidence refresh: circuit open until %s (%s)", circuit["until"], stop_reason)
        return {"result": R_STOPPED if stop_reason else R_COMPLETED, "mode": m, "stop_reason": stop_reason, "attempts": attempts, "circuit": circuit}

    def status(self) -> dict:
        return {"version": VERSION, "mode": self.mode_fn(), "owner": self.owner, "lease": LEASE_NAME, "runs": self.runs,
                "last_run_at": self.last_run_at, "last_result": self.last_result, "last_error": self.last_error, "totals": dict(self.totals),
                "history": list(self.history)[:20], "spacing_s": self.spacing_s, "journal": self.journal.describe(self.clock()), "disclaimer": DISCLAIMER}
