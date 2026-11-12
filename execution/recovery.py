"""
execution/recovery.py - bot-position recovery: the way back after an operator closed or reduced a bot position on Alpaca's website
(BOT_POSITION_RECOVERY_DESIGN.md revision 3.5; the operator's decisions R-1 to R-5 of 2026-11-09: a blocker before attended Paper Auto, a
LOCAL COMMAND with no HTTP route and no button, a visible intervention note, C-8 retained).

  python -m execution.recovery preview --position-id <id>
  python -m execution.recovery apply   --position-id <id> --evidence-hash <hash> --approved-by <name> --reason <text> [--i-confirmed-flat-on-the-website]

Run it from the server's app folder, with the server's env file: the database is that checkout's file (config.RESEARCH_DB_PATH, the file the
server opens). It refuses when the file does not already exist and never creates one.

DATABASE ROWS ONLY (design section 3): no stream, no broker, no network client, no HTTP route. It places, cancels and changes no broker order.
It neither reads nor changes Emergency Stop, Pause or the daily-loss halt. It reads the execution mode only to refuse while Paper Auto is on, and
never changes it. Every read is strict: a missing table or column or any SQLite error refuses, it never passes. The one exception: absent Stage
4.6B tables mean "no intents" (they exist only after the first Link). `preview` writes nothing. `apply` re-derives every fact from the newest
rows inside ONE BEGIN IMMEDIATE, refuses unless the evidence hash and all twelve preconditions hold, and writes the position with compare-and-set
on state, quantity, attention_reason and updated_at, with its MANUAL_RECOVERY event (and a CLOSED event for a close). The evidence hash comes from
`preview` only: a refused `apply` never prints one. Exit 0 = done; 1 = refused (every failing check printed); 2 = a usage error (a missing, empty,
over-long or multi-line argument), a read error or any unexpected error. A refusal or an error inside the transaction writes nothing.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

import config
from execution import INTENT_OPEN
from paper import alpaca_order_rules as RU                 # the Stage 4.6B state sets only (pure constants of the frozen module; no I/O)
from setups.market_hours import is_regular_session

RECOVERABLE_REASONS = ("BROKER_FLAT_INTERNAL_OPEN", "QTY_MISMATCH", "EXIT_ATTEMPTS_EXHAUSTED")
QTY_FINDINGS = ("QTY_MISMATCH", "BROKER_FLAT_INTERNAL_OPEN")
MANUAL_INTENT_STATES = tuple(sorted(RU.UNRESOLVED | RU.LIVE))
OPEN_ORDERS_LIMIT = 500                                     # account_refresh/service.py reads at most 500 open orders: a list that long may be cut
PARTIAL_MAX_AGE_S = 600                                     # precondition 6 for a partial
EXECUTOR_MAX_AGE_S = 600                                    # precondition 11
TEXT_MAX = 500                                              # --approved-by and --reason: longer input is refused, never cut
EXIT_REASON = "MANUAL_EXTERNAL"                             # its text is in execution.EXIT_TEXT; deliberately NOT in EXIT_REASONS (not a strategy exit)
EVENT_KIND = "MANUAL_RECOVERY"
PRECONDITIONS = ((1, "NOT_RECOVERABLE"), (2, "PAPER_AUTO_ON"), (3, "INTENT_OPEN"), (4, "MANUAL_INTENT_ACTIVE"), (5, "SNAPSHOT_NOT_TRUSTED"), (6, "SNAPSHOT_TOO_OLD"),
                 (7, "SNAPSHOT_OLDER_THAN_FINDING"), (8, "OPEN_ORDER_IN_SYMBOL"), (9, "BROKER_QTY_NOT_RECOVERABLE"), (10, "FINDING_DISAGREES"), (11, "EXECUTOR_NOT_RUNNING"),
                 (12, "EXIT_ATTEMPTS_USED"))
FINDING_TEXT = "Wait for the next cycle. If it still refuses, restart the server."
EXIT_ATTEMPTS_TEXT = ("The automatic exit attempts of this position are used up, so a remainder could never exit automatically: close the rest on Alpaca's "
                      "website, then run apply for a close.")
SELL_TEXT = "A SELL of the remainder can follow in the next cycle if any exit rule is met."
SELL_NOTE = ("Monitor only and Emergency Stop do not prevent that SELL; outside regular hours it can come at the first cycle of the next session, "
             "whoever is watching.")
ARM_TEXT = ("Paper Auto stays off until you enable it, or until the scheduled arm does if PAPER_AUTO_RUN_POLICY=SCHEDULED_ARM and "
            "PAPER_AUTO_SCHEDULED_ARM_ENABLED=true are both set (both are off by default).")
ATTEST_TEXT = ("--i-confirmed-flat-on-the-website is your attestation that you looked at Alpaca's positions page for {sym} after preview. It is an "
               "attestation, not evidence: the command never reads the broker.")


class RecoveryReadError(RuntimeError):
    """A strict read failed: a missing table or column, or any SQLite error. The command refuses; it never passes."""


# ---------------------------------------------------------------- helpers ------------------------------------------------------------------
def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds")


def _parse(ts) -> Optional[datetime]:
    if not ts:
        return None
    try:
        d = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except ValueError:
        return None
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def _rows(conn: sqlite3.Connection, sql: str, args=(), *, absent_ok: bool = False) -> List[dict]:
    """Strict: any SQLite error raises RecoveryReadError. `absent_ok` (the Stage 4.6B tables only): a missing TABLE reads as no rows."""
    try:
        return [dict(r) for r in conn.execute(sql, args).fetchall()]
    except sqlite3.OperationalError as exc:
        if absent_ok and "no such table" in str(exc):
            return []
        raise RecoveryReadError(f"{type(exc).__name__}: {exc}") from None
    except sqlite3.Error as exc:
        raise RecoveryReadError(f"{type(exc).__name__}: {exc}") from None


def evidence_hash(position_id: str, internal_qty: int, broker_qty: int, attention_reason: Optional[str], updated_at: Optional[str]) -> str:
    """Design section 5 step 3: SHA-256 of the position id, both quantities, the attention reason and the position's updated_at. It leaves out
    the snapshot's time, which moves at every refresh and would refuse apply while the operator checks the website."""
    body = json.dumps({"position_id": position_id, "internal_qty": internal_qty, "broker_qty": broker_qty, "attention_reason": attention_reason,
                       "updated_at": updated_at}, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def _broker_qty(positions: List[Any], symbol: str) -> Tuple[Optional[int], str]:
    """The snapshot's quantity in `symbol` (absent = 0) for precondition 9, or (None, why): only a whole number on a long is recoverable."""
    rows = [p for p in positions if isinstance(p, dict) and str(p.get("symbol") or "").upper() == symbol]
    if not rows:
        return 0, "absent from the broker positions (flat)"
    if len(rows) > 1:
        return None, f"{len(rows)} broker position rows for {symbol}"
    p = rows[0]
    side = str(p.get("side") or "").lower()
    try:
        q = Decimal(str(p.get("qty")))
    except (InvalidOperation, ValueError):
        return None, "the broker quantity is not a number"
    if not q.is_finite():
        return None, "the broker quantity is not a number"
    if side != "long" or q < 0:
        return None, f"the broker position is {side or 'of unknown side'} (only a long is recoverable)"
    if q != q.to_integral_value():
        return None, f"the broker quantity {format(q, 'f')} is a fraction"
    return int(q), f"{int(q)} share(s) long"


# ---------------------------------------------------------------- the facts and the twelve preconditions ------------------------------------
def derive(conn: sqlite3.Connection, position_id: str, now: datetime) -> Dict[str, Any]:
    """Every fact the command uses, from rows (strict), and each precondition's result (design section 4). Reads only."""
    checks = {n: {"n": n, "code": code, "ok": None, "detail": "not evaluated"} for n, code in PRECONDITIONS}

    def res(n: int, ok: bool, detail: str) -> None:
        checks[n].update(ok=bool(ok), detail=detail)
    f: Dict[str, Any] = {"position_id": position_id, "now": _iso(now), "position": None, "symbol": None, "case": None, "internal_qty": None, "broker_qty": None,
                         "mode": None, "snapshot": None, "evidence_hash": None}
    pos = _rows(conn, "SELECT position_id, symbol, side, quantity, entry_qty, state, attention_reason, exit_reason, exit_cycle, exit_qty, realized_pnl, updated_at "
                      "FROM paper_positions WHERE position_id = ?", (position_id,))
    if not pos:
        res(1, False, f"no bot position {position_id}")
        return _done(f, checks)
    p = pos[0]
    sym = str(p["symbol"]).upper()
    f.update(position=p, symbol=sym, internal_qty=int(p["quantity"]))
    res(1, p["state"] == "ATTENTION" and p["attention_reason"] in RECOVERABLE_REASONS,
        f"state {p['state']}, attention reason {p['attention_reason'] or 'none'}" + ("" if p["state"] == "ATTENTION" and p["attention_reason"] in RECOVERABLE_REASONS
                                                                                     else f" (recoverable: ATTENTION with {', '.join(RECOVERABLE_REASONS)})"))
    m = _rows(conn, "SELECT mode FROM execution_mode WHERE id = 1")
    f["mode"] = m[0]["mode"] if m else "MONITOR_ONLY"                                   # no row: the executor's own default (ExecutionStore.mode())
    res(2, f["mode"] == "MONITOR_ONLY", f"execution mode {f['mode']}" + ("" if f["mode"] == "MONITOR_ONLY" else ": set Monitor only first"))
    its = _rows(conn, f"SELECT execution_id, intent_type, state FROM execution_intents WHERE state IN ({', '.join('?' * len(INTENT_OPEN))}) AND (position_id = ? OR symbol = ?)",
                (*sorted(INTENT_OPEN), position_id, sym))
    res(3, not its, "no open execution intent" if not its else "open: " + ", ".join(f"{i['intent_type']} {i['execution_id'][:8]} {i['state']}" for i in its))
    snap_rows = _rows(conn, "SELECT refresh_id, refreshed_at, positions_json, open_orders_json FROM account_refresh_snapshot WHERE id = 1")
    live = _rows(conn, "SELECT consecutive_failures FROM account_refresh_liveness WHERE id = 1")
    snap, positions, orders, attempt = None, None, None, None
    if snap_rows:
        snap = snap_rows[0]
        try:
            positions, orders = json.loads(snap["positions_json"] or "null"), json.loads(snap["open_orders_json"] or "null")
        except (TypeError, ValueError):
            positions, orders = None, None
        a = _rows(conn, "SELECT started_at FROM account_refresh_attempts WHERE refresh_id = ? AND result = 'SUCCESS'", (snap["refresh_id"],))
        attempt = _parse(a[0]["started_at"]) if len(a) == 1 else None
    failures = live[0]["consecutive_failures"] if live else None
    readable = isinstance(positions, list) and isinstance(orders, list)
    if snap is None:
        res(5, False, "no account snapshot")
    elif not readable:
        res(5, False, "the account snapshot's positions or orders are unreadable")
    elif type(failures) is not int or failures != 0:
        res(5, False, f"the account refresh has {failures if failures is not None else 'unknown'} consecutive failure(s)")
    else:
        res(5, True, "account snapshot present, no refresh failure")
    refreshed = _parse(snap["refreshed_at"]) if snap else None
    age = (now - refreshed).total_seconds() if refreshed else None
    f["snapshot"] = {"refreshed_at": snap["refreshed_at"] if snap else None, "age_s": int(age) if age is not None else None,
                     "attempt_started_at": _iso(attempt) if attempt else None}
    # precondition 9: the broker's quantity (decides the case)
    bq, why = _broker_qty(positions, sym) if readable else (None, "no readable account snapshot")
    internal = f["internal_qty"]
    if bq is not None and bq == 0:
        f["case"] = "CLOSE"
    elif bq is not None and 0 < bq < internal:
        f["case"] = "PARTIAL"
    f["broker_qty"] = bq
    res(9, f["case"] is not None, f"broker: {why}; internal: {internal}" + ("" if f["case"] is not None or bq is None else " (the broker must hold fewer shares than the record)"))
    # precondition 6: the snapshot's age (a close: two refresh intervals; a partial: 600 s)
    if f["case"] == "PARTIAL":
        limit, basis = PARTIAL_MAX_AGE_S, "a partial"
    elif is_regular_session(now):
        limit, basis = 2 * float(config.ACCOUNT_REFRESH_SECONDS), "a close in the regular session: two refresh intervals"
    else:
        limit, basis = 2 * float(config.ACCOUNT_REFRESH_IDLE_SECONDS), "a close outside the regular session: two refresh intervals"
    if age is None:
        res(6, False, "no snapshot time")
    elif age < 0:
        res(6, False, f"the snapshot is dated {int(-age)} s in the future")
    else:
        res(6, age <= limit, f"snapshot {int(age)} s old (limit {int(limit)} s for {basis})")
    # precondition 7 and 10: the latest finding of the position
    fd = _rows(conn, "SELECT at, kind, detail_json FROM execution_reconciliations WHERE position_id = ? ORDER BY at DESC, rowid DESC LIMIT 1", (position_id,))
    finding = fd[0] if fd else None
    updated = _parse(p["updated_at"])
    f_at = _parse(finding["at"]) if finding else None
    if attempt is None or updated is None or (finding is not None and f_at is None):
        res(7, False, "the snapshot's attempt, the position's updated_at or the finding's time is unknown")
    else:
        ok7 = attempt > updated and (f_at is None or attempt > f_at)
        res(7, ok7, f"snapshot attempt started {_iso(attempt)}; position updated {_iso(updated)}" + (f"; latest finding {_iso(f_at)}" if f_at else "; no finding"))
    # precondition 4: Stage 4.6B (absent tables: no intents)
    man = _rows(conn, f"SELECT intent_id, state FROM alpaca_paper_order_intents WHERE symbol = ? AND state IN ({', '.join('?' * len(MANUAL_INTENT_STATES))})",
                (sym, *MANUAL_INTENT_STATES), absent_ok=True)
    evs = _rows(conn, "SELECT e.at FROM alpaca_paper_order_events e JOIN alpaca_paper_order_intents i ON i.intent_id = e.intent_id WHERE i.symbol = ?", (sym,), absent_ok=True)
    later = [e["at"] for e in evs if _parse(e["at"]) is None or attempt is None or _parse(e["at"]) >= attempt]   # the same second counts (whole-second stamps)
    res(4, not man and not later, "no unresolved or live manual order and no manual order event since the snapshot's attempt began" if not man and not later else
        "; ".join(([f"manual intent {r['intent_id']} {r['state']}" for r in man]) + ([f"{len(later)} manual order event(s) since the snapshot's attempt began"] if later else [])))
    # precondition 8: open orders in the snapshot
    if not readable:
        res(8, False, "no readable account snapshot")
    else:
        mine = [o for o in orders if isinstance(o, dict) and str(o.get("symbol") or "").upper() == sym]
        res(8, not mine and len(orders) < OPEN_ORDERS_LIMIT,
            f"{len(mine)} open order(s) in {sym}; {len(orders)} open order(s) in the snapshot" + (f" (at the {OPEN_ORDERS_LIMIT} limit: the list may be cut)" if len(orders) >= OPEN_ORDERS_LIMIT else ""))
    # precondition 10: the latest finding agrees on the broker quantity
    if finding is None:
        res(10, False, "no reconciliation finding for this position. " + FINDING_TEXT)
    else:
        try:
            det = json.loads(finding["detail_json"] or "{}")
        except (TypeError, ValueError):
            det = None
        fq = None
        if isinstance(det, dict) and finding["kind"] in QTY_FINDINGS:
            raw = det.get("broker_qty", 0 if finding["kind"] == "BROKER_FLAT_INTERNAL_OPEN" else None)
            fq = raw if type(raw) is int else None
        ok10 = fq is not None and bq is not None and fq == bq
        if p["attention_reason"] == "EXIT_ATTEMPTS_EXHAUSTED":
            ok10 = ok10 and f_at is not None and updated is not None and f_at >= updated
        res(10, ok10, f"latest finding {finding['kind']} at {finding['at']}: broker {fq if fq is not None else 'unknown'}; snapshot broker {bq if bq is not None else 'unknown'}"
            + ("" if ok10 else ". " + FINDING_TEXT))
    # precondition 11: a live executor is reconciling
    run = _rows(conn, "SELECT started_at FROM execution_runs WHERE result = 'RUN' AND (error IS NULL OR error = '') ORDER BY started_at DESC, rowid DESC LIMIT 1")
    r_at = _parse(run[0]["started_at"]) if run else None
    r_age = (now - r_at).total_seconds() if r_at else None
    res(11, r_age is not None and 0 <= r_age <= EXECUTOR_MAX_AGE_S,
        "no executor run" if r_age is None else f"latest executor run {int(r_age)} s ago (limit {EXECUTOR_MAX_AGE_S} s)" + ("" if 0 <= r_age <= EXECUTOR_MAX_AGE_S else ": is the server running?"))
    # precondition 12: a partial keeps an automatic exit
    max_cycles = int(getattr(config, "EXECUTION_MAX_EXIT_CYCLES", 3))
    if f["case"] == "PARTIAL":
        res(12, int(p["exit_cycle"]) < max_cycles, f"exit cycle {p['exit_cycle']} of {max_cycles}" + ("" if int(p["exit_cycle"]) < max_cycles else ". " + EXIT_ATTEMPTS_TEXT))
    else:
        res(12, True, "a close" if f["case"] == "CLOSE" else "not a partial")
    if f["case"] is not None:                                                          # a hash only for a close or a partial (precondition 9's case)
        f["evidence_hash"] = evidence_hash(position_id, internal, bq, p["attention_reason"], p["updated_at"])
    return _done(f, checks)


def _done(f: Dict[str, Any], checks: Dict[int, dict]) -> Dict[str, Any]:
    """Every precondition must be True; one that could not be evaluated (no such position) refuses as NOT_RECOVERABLE, never passes."""
    f["checks"] = [checks[n] for n, _ in PRECONDITIONS]
    if f["position"] is None:
        f["refusals"] = ["NOT_RECOVERABLE"]
    else:
        f["refusals"] = [c["code"] for c in f["checks"] if c["ok"] is not True]
    return f


def _will_change(f: Dict[str, Any]) -> Optional[dict]:
    if f["case"] == "CLOSE":
        return {"state": "ATTENTION -> CLOSED", "quantity": f"{f['internal_qty']} -> 0", "exit_reason": f"{f['position']['exit_reason'] or 'none'} -> {EXIT_REASON}",
                "attention_reason": f"{f['position']['attention_reason']} -> none", "closed_at": "set", "exit_qty and realized_pnl": "unchanged",
                "events": [EVENT_KIND, "CLOSED"]}
    if f["case"] == "PARTIAL":
        return {"state": "ATTENTION (unchanged)", "quantity": f"{f['internal_qty']} -> {f['broker_qty']}", "attention_reason": f"{f['position']['attention_reason']} -> QTY_MISMATCH",
                "exit_reason, exit_qty and realized_pnl": "unchanged", "events": [EVENT_KIND],
                "next cycle": "the quantities agree, so the server clears the attention (ATTENTION_CLEARED) and the remainder regains its automatic exit rules"}
    return None


# ---------------------------------------------------------------- connections ----------------------------------------------------------
def _connect(path: Path, mode: str) -> sqlite3.Connection:
    """mode=ro or mode=rw: SQLite never creates the file either way (the caller has already refused a missing one)."""
    conn = sqlite3.connect(f"file:{Path(path).as_posix()}?mode={mode}", uri=True, timeout=10.0, isolation_level=None)
    conn.row_factory = sqlite3.Row
    return conn


def _missing(path: Path) -> Optional[dict]:
    if not Path(path).exists():
        return {"ok": False, "errors": [f"database file missing: {Path(path).name}. Run the command from the server's app folder; it never creates a database."]}
    return None


def settings() -> dict:
    """The limits THIS process applies (this checkout's config): preview prints them, so the operator can compare them with the server's
    settings when the server runs with an overrides file."""
    return {"ACCOUNT_REFRESH_SECONDS": float(config.ACCOUNT_REFRESH_SECONDS), "ACCOUNT_REFRESH_IDLE_SECONDS": float(config.ACCOUNT_REFRESH_IDLE_SECONDS),
            "EXECUTION_MAX_EXIT_CYCLES": int(getattr(config, "EXECUTION_MAX_EXIT_CYCLES", 3)), "partial_max_age_s": PARTIAL_MAX_AGE_S,
            "executor_max_age_s": EXECUTOR_MAX_AGE_S}


# ---------------------------------------------------------------- the two commands -----------------------------------------------------
def preview(path: Path, position_id: str, now: Optional[datetime] = None) -> dict:
    """Design section 5 step 2: every fact and each precondition's result, and evidence_hash. Writes nothing."""
    now = now or datetime.now(timezone.utc)
    miss = _missing(path)
    if miss:
        return {**miss, "command": "preview", "read_error": True}
    conn = _connect(path, "ro")
    try:
        conn.execute("BEGIN")                                                          # one read transaction: every fact from one consistent view
        f = derive(conn, position_id, now)
        conn.execute("COMMIT")
    finally:
        conn.close()
    notes = []
    if f["symbol"]:
        age = f["snapshot"]["age_s"]
        notes.append(f"Do not trade {f['symbol']} on Alpaca's website between this preview and the first cycle after apply. "
                     f"The account snapshot is {age if age is not None else 'of unknown age'}{' s old' if age is not None else ''}.")
    if f["case"] == "PARTIAL":
        notes += [SELL_TEXT, SELL_NOTE]
    if f["case"] == "CLOSE":
        notes.append("A close needs --i-confirmed-flat-on-the-website: look at Alpaca's positions page for " + f["symbol"] + " first.")
    return {"ok": not f["refusals"], "command": "preview", "writes": "none", "database": str(path), "position_id": position_id, "symbol": f["symbol"], "case": f["case"],
            "internal_qty": f["internal_qty"], "broker_qty": f["broker_qty"], "attention_reason": (f["position"] or {}).get("attention_reason"),
            "state": (f["position"] or {}).get("state"), "exit_reason": (f["position"] or {}).get("exit_reason"), "exit_cycle": (f["position"] or {}).get("exit_cycle"),
            "mode": f["mode"], "snapshot": f["snapshot"], "will_change": _will_change(f) if not f["refusals"] else None, "preconditions": f["checks"],
            "refusals": f["refusals"], "evidence_hash": f["evidence_hash"], "settings": settings(), "notes": notes}


def apply(path: Path, position_id: str, evidence: str, approved_by: str, reason: str, confirmed_flat: bool = False, now: Optional[datetime] = None, *,
          _before_write: Optional[Callable[[sqlite3.Connection], None]] = None) -> dict:
    """Design section 5 step 5: in ONE BEGIN IMMEDIATE, re-derive every fact from the newest rows; refuse if the evidence hash differs, any
    precondition fails or (a close) the website confirmation is missing; then write with compare-and-set. All or nothing.
    `_before_write` is a test seam only: it runs on the transaction's connection right before the compare-and-set write."""
    now = now or datetime.now(timezone.utc)
    actor, why = (approved_by or "").strip(), (reason or "").strip()
    errors = []
    if not actor:
        errors.append("--approved-by is required")
    if not why:
        errors.append("--reason is required")
    if len(actor) > TEXT_MAX or len(why) > TEXT_MAX:
        errors.append(f"--approved-by and --reason are at most {TEXT_MAX} characters")
    if any(ord(c) < 32 or ord(c) == 127 for c in actor + why):
        errors.append("--approved-by and --reason are one line of text (no control characters): the report prints them as they are")
    if not (evidence or "").strip():
        errors.append("--evidence-hash is required (run preview first)")
    if errors:
        return {"ok": False, "command": "apply", "refusals": ["ARGUMENTS"], "errors": errors, "written": False, "usage_error": True}
    miss = _missing(path)
    if miss:
        return {**miss, "command": "apply", "read_error": True, "written": False}
    stamp = _iso(now)
    conn = _connect(path, "rw")
    try:
        conn.execute("BEGIN IMMEDIATE")
        try:
            f = derive(conn, position_id, now)
            refusals = list(f["refusals"])
            if f["evidence_hash"] is None or f["evidence_hash"] != evidence.strip():
                refusals.insert(0, "EVIDENCE_HASH_MISMATCH")
            if f["case"] == "CLOSE" and not confirmed_flat:
                refusals.append("CONFIRMATION_REQUIRED")
            if refusals:
                conn.execute("ROLLBACK")
                return {"ok": False, "command": "apply", "written": False, "refusals": refusals, "preconditions": f["checks"],   # no hash here: a new one comes from a new preview only
                        "errors": (["the evidence hash differs from the newest rows: something changed since preview; run preview again"] if "EVIDENCE_HASH_MISMATCH" in refusals else [])
                        + ([ATTEST_TEXT.format(sym=f["symbol"]) + " Without it a close is refused."] if "CONFIRMATION_REQUIRED" in refusals else [])}
            p = f["position"]
            if _before_write is not None:
                _before_write(conn)
            detail = {"actor": actor, "reason": why, "evidence_hash": f["evidence_hash"], "internal_qty": f["internal_qty"], "broker_qty": f["broker_qty"],
                      "original_exit_reason": p["exit_reason"], "attention_reason": p["attention_reason"], "case": f["case"],
                      "snapshot_refreshed_at": f["snapshot"]["refreshed_at"], "confirmed_flat_on_website": bool(confirmed_flat) if f["case"] == "CLOSE" else None}
            cas = (position_id, int(p["quantity"]), p["attention_reason"], p["updated_at"])
            if f["case"] == "CLOSE":
                cur = conn.execute("UPDATE paper_positions SET state = 'CLOSED', quantity = 0, exit_reason = ?, closed_at = ?, attention_reason = NULL, updated_at = ? "
                                   "WHERE position_id = ? AND state = 'ATTENTION' AND quantity = ? AND attention_reason = ? AND updated_at = ?", (EXIT_REASON, stamp, stamp, *cas))
            else:
                cur = conn.execute("UPDATE paper_positions SET quantity = ?, attention_reason = 'QTY_MISMATCH', updated_at = ? "
                                   "WHERE position_id = ? AND state = 'ATTENTION' AND quantity = ? AND attention_reason = ? AND updated_at = ?", (f["broker_qty"], stamp, *cas))
            if cur.rowcount != 1:
                conn.execute("ROLLBACK")
                return {"ok": False, "command": "apply", "written": False, "refusals": ["POSITION_CHANGED"],
                        "errors": ["the position changed during apply (compare-and-set failed): nothing was written; run preview again"]}
            to_state = "CLOSED" if f["case"] == "CLOSE" else "ATTENTION"
            ins = "INSERT INTO position_events (event_id, position_id, at, kind, from_state, to_state, reason, detail_json) VALUES (?,?,?,?,?,?,?,?)"
            conn.execute(ins, (uuid.uuid4().hex, position_id, stamp, EVENT_KIND, "ATTENTION", to_state,
                               "position record corrected: the broker is flat" if f["case"] == "CLOSE" else f"position record corrected: the broker holds {f['broker_qty']} share(s)",
                               json.dumps(detail, sort_keys=True, separators=(",", ":"), default=str)))
            if f["case"] == "CLOSE":
                conn.execute(ins, (uuid.uuid4().hex, position_id, stamp, "CLOSED", "ATTENTION", "CLOSED", EXIT_REASON,
                                   json.dumps({"source": EVENT_KIND, "exit_qty": p["exit_qty"], "evidence_hash": f["evidence_hash"]}, sort_keys=True, separators=(",", ":"))))
            conn.execute("COMMIT")
        except BaseException:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise
    finally:
        conn.close()
    notes = [ATTEST_TEXT.format(sym=f["symbol"]),
             f"Confirm on Alpaca's website that {f['symbol']} is flat. If you hold shares, sell them there: the app has no record of them and they have no automatic exit."] \
        if f["case"] == "CLOSE" else [SELL_TEXT, SELL_NOTE]
    notes += ["Watch the first cycle after apply, without restarting the server before it, and read the Risk card.", ARM_TEXT]
    return {"ok": True, "command": "apply", "written": True, "database": str(path), "case": f["case"], "position_id": position_id, "symbol": f["symbol"], "at": stamp,
            "internal_qty": f["internal_qty"], "broker_qty": f["broker_qty"], "evidence_hash": f["evidence_hash"], "events": [EVENT_KIND] + (["CLOSED"] if f["case"] == "CLOSE" else []),
            "notes": notes}


def main(argv=None, path: Optional[Path] = None, now: Optional[datetime] = None) -> int:
    """The CLI. Exit 0 = done; 1 = refused (each failing check printed); 2 = a usage error, a read error or any unexpected error (printed, never a
    traceback that reads as a refusal). Prints JSON. `path` and `now` are test seams."""
    import argparse
    import sys
    p = argparse.ArgumentParser(prog="python -m execution.recovery", description="Bot-position recovery after a manual exit on Alpaca's website (database rows only)")
    sub = p.add_subparsers(dest="cmd", required=True)
    pv = sub.add_parser("preview")
    pv.add_argument("--position-id", required=True)
    ap = sub.add_parser("apply")
    ap.add_argument("--position-id", required=True)
    ap.add_argument("--evidence-hash", required=True)
    ap.add_argument("--approved-by", required=True)
    ap.add_argument("--reason", required=True)
    ap.add_argument("--i-confirmed-flat-on-the-website", dest="confirmed_flat", action="store_true")
    a = p.parse_args(argv)
    db = Path(path) if path is not None else Path(config.RESEARCH_DB_PATH)
    try:
        if a.cmd == "preview":
            out = preview(db, a.position_id, now)
        else:
            out = apply(db, a.position_id, a.evidence_hash, a.approved_by, a.reason, a.confirmed_flat, now)
    except RecoveryReadError as exc:
        out = {"ok": False, "command": a.cmd, "errors": [f"read error: {exc}"[:300]], "read_error": True}
    except sqlite3.Error as exc:
        out = {"ok": False, "command": a.cmd, "errors": [f"database error: {type(exc).__name__}: {exc}"[:300]], "read_error": True}
    except Exception as exc:  # noqa: BLE001 - e.g. a hand-edited row of the wrong type: refused with the error, nothing written (apply rolled back)
        out = {"ok": False, "command": a.cmd, "errors": [f"error: {type(exc).__name__}: {exc}"[:300]], "read_error": True}
    print(json.dumps(out, indent=1, sort_keys=True, default=str))
    sys.stdout.flush()
    if out.get("ok"):
        return 0
    return 2 if out.get("read_error") or out.get("usage_error") else 1


if __name__ == "__main__":
    raise SystemExit(main())
