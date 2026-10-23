"""
scripts/run_aapl_paper_smoke.py - the Stage 6.0 supervised one-share AAPL Alpaca PAPER smoke, standalone (no VS Code, no Claude Code).

HARD SCOPE (permanent): symbol AAPL, BUY then SELL only, 1 share, at most 2 broker order submissions in total, Alpaca PAPER host only.
No Robinhood, no live endpoint, no model call, no manual order: every broker write is made by the REAL Stage 6.0 executor
(setup -> proposal -> safety gate -> executor -> Alpaca PAPER) and passes through an independent order guard first.

What it does, in order (any failure before the first order = NO ORDER; Paper Auto is always switched off in `finally`):
  1. idempotency: an OS lock + a persisted run marker; a run that reached RUNNING / PASS / FAIL-after-submission is never repeated
  2. time window: 09:45 <= New York time < 10:00 on --run-date, else NO ORDER (no catch-up later)
  3. the normal Stock Agent server must answer and be in Monitor only (it must not trade at the same time)
  4. fresh pre-flight: paper snapshot refresh, paper proof, account active / not blocked, equity, buying power, broker clock open,
     AAPL tradable, fresh AAPL quote, safety gate Trading allowed, reconciliation healthy, no AAPL order / position / earlier smoke
     order today, share cap 1, a fresh scratch database with Paper Auto off
  5. SMOKE_FORCED_SETUP: AAPL forced WATCH -> READY through the real setup service (infrastructure proof, not a strategy signal);
     the real proposal must be REVIEWABLE and eligible
  6. Paper Auto on through the real execution service; one executor cycle submits the entry (capped to 1 share); an ambiguous
     result is reconciled by client_order_id only (never resent); the fill must be broker-confirmed
  7. SMOKE_FORCED_EXIT: the setup's stretch is forced to normalize; the executor decides the exit and submits the one SELL
  8. final reconciliation; PASS only if every condition holds; result JSON + log; secret scan; Paper Auto off

Exit codes: 0 PASS, 1 FAIL, 2 NO ORDER (refused before any broker write), 3 REFUSED (duplicate / already ran).
--simulated runs the same path against the deterministic simulated paper broker and simulated market data (offline proof).
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
import time
import traceback
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

SYMBOL = "AAPL"
MAX_QTY = 1
MAX_ORDERS = 2
SEQUENCE = ("buy", "sell")
PAPER_HOST = "paper-api.alpaca.markets"
ALPACA_NAMES = ("ALPACA_API_KEY", "ALPACA_SECRET_KEY", "ALPACA_PAPER_API_KEY", "ALPACA_PAPER_SECRET_KEY")
KEEP_OUT_PATTERN = re.compile(r"(ROBINHOOD|ANTHROPIC|OPENAI|FRED_|SEC_USER_AGENT|_PASSWORD|_TOKEN|API_KEY|SECRET)", re.I)
WINDOW_ET = ("09:45", "10:00")
QUOTE_MAX_AGE_S = 120.0
FILL_TIMEOUT_S = 120.0
AMBIGUOUS_TIMEOUT_S = 150.0
POLL_S = 3.0
LABEL_SETUP, LABEL_EXIT = "SMOKE_FORCED_SETUP", "SMOKE_FORCED_EXIT"
PNL_EXCEPTION = "UNADJUSTED_PNL_ACCEPTED_FOR_SMOKE_ONLY"
DB_FILE, STATE_FILE, RESULT_FILE, LOCK_FILE, LOG_FILE = "aapl_paper_smoke.db", "aapl_paper_smoke_state.json", "aapl_paper_smoke_result.json", "aapl_paper_smoke.lock", "aapl_paper_smoke.log"
READS_DB_FILE = "aapl_paper_smoke_reads.db"                        # the app-wide database singleton (PPE / local-paper readers) in this process
EXIT_PASS, EXIT_FAIL, EXIT_NO_ORDER, EXIT_REFUSED = 0, 1, 2, 3
WATCH = dict(stretch=-67.0, confidence=34, demand=41)
READY = dict(stretch=-54.0, confidence=58, demand=72)
NORMALIZED = dict(stretch=-10.0, confidence=58, demand=72)

LOG = logging.getLogger("aapl_smoke")


class NoOrder(Exception):
    """A pre-flight / window / server / guard refusal before any broker write."""


class SmokeFail(Exception):
    """A failure after the smoke started trading (FAIL; never a retry)."""


class SmokeGuardViolation(Exception):
    """The independent order guard refused an order BEFORE it reached the broker."""


# ---------------------------------------------------------------- redaction ----------------------------------------------------------
class Redactor:
    def __init__(self, secrets=()):
        self.values = sorted({s for s in secrets if isinstance(s, str) and len(s) >= 6}, key=len, reverse=True)

    def __call__(self, text: str) -> str:
        for v in self.values:
            if v in text:
                text = text.replace(v, "***")
        return text


class _RedactFormatter(logging.Formatter):
    def __init__(self, redact: Redactor):
        super().__init__("%(asctime)s %(levelname)s %(name)s: %(message)s")
        self.redact = redact

    def format(self, record):
        return self.redact(super().format(record))


def select_credentials(env_file: Path) -> Dict[str, str]:
    """ONLY the four Alpaca names the paper client and the market data client need; nothing else is read into the process."""
    from dotenv import dotenv_values
    vals = dotenv_values(env_file)
    return {n: vals[n] for n in ALPACA_NAMES if vals.get(n)}


# ---------------------------------------------------------------- the order guard --------------------------------------------------------
class OrderGuard:
    """Wraps the broker client's single submission seam. Refuses before the wire: another symbol, a quantity other than 1, a third order,
    a SELL first / a BUY second, a non-paper host, a client order id that is not the executor's. Raises; never sends."""

    def __init__(self, broker):
        from execution import broker as BK
        self.BK = BK
        self.broker = broker
        self.sent: List[Dict[str, Any]] = []
        self.violation: Optional[str] = None
        self._orig = broker.post_order
        broker.post_order = self.post

    def check(self, body: dict, coid: str) -> Optional[str]:
        n = len(self.sent)
        if self.BK.PAPER_HOST != PAPER_HOST or self.BK.BASE != f"https://{PAPER_HOST}":
            return "broker host is not paper-api.alpaca.markets"
        if n >= MAX_ORDERS:
            return f"order {n + 1} refused: at most {MAX_ORDERS} broker order submissions"
        if body.get("symbol") != SYMBOL:
            return f"symbol {body.get('symbol')} refused: AAPL only"
        if body.get("qty") != str(MAX_QTY):
            return f"quantity {body.get('qty')} refused: exactly 1 share"
        if body.get("side") != SEQUENCE[n]:
            return f"order {n + 1} side {body.get('side')} refused: the sequence is BUY then SELL"
        if body.get("type") != "market" or body.get("time_in_force") != "day":
            return "only market day orders"
        prefix = "stockagent-entry-" if n == 0 else "stockagent-exit-"
        if not str(coid).startswith(prefix) or body.get("client_order_id") != coid:
            return f"client order id {coid} is not the executor's {prefix}..."
        return None

    def post(self, payload: bytes, coid: str):
        try:
            body = json.loads(payload)
        except ValueError:
            body = {}
        reason = self.check(body, coid)
        if reason is not None:
            self.violation = reason
            LOG.error("ORDER GUARD refused before the wire: %s", reason)
            raise SmokeGuardViolation(reason)
        self.sent.append({"side": body["side"], "qty": body["qty"], "symbol": body["symbol"], "client_order_id": coid})
        res = self._orig(payload, coid)
        LOG.info("ORDER SENT %d/%d: %s %s %s %s %s -> %s %s", len(self.sent), MAX_ORDERS, body["side"].upper(), body["qty"], body["symbol"], body["type"],
                 body["time_in_force"], res.get("kind"), res.get("status") or res.get("reason") or "")
        return res

    def count(self, side: str) -> int:
        return sum(1 for o in self.sent if o["side"] == side)


# ---------------------------------------------------------------- run context (production or simulated) ----------------------------------
@dataclass
class Ctx:
    broker: Any
    market: Any
    clock: Callable[[], datetime]
    sleep: Callable[[float], None]
    server_check: Callable[[], Dict[str, Any]]
    refresh_snapshot: Optional[Callable[[], Dict[str, Any]]]
    simulated: bool
    runtime_dir: Path
    log_dir: Path
    run_date: str
    secrets: List[str] = field(default_factory=list)
    hooks: Dict[str, Callable] = field(default_factory=dict)
    sim: Any = None
    loaded_names: List[str] = field(default_factory=list)


def et_now(dt: datetime) -> datetime:
    from zoneinfo import ZoneInfo
    return dt.astimezone(ZoneInfo("America/New_York"))


class _NoPositions:
    def position(self, symbol):
        from trader_view.models import PositionSource, PositionState
        return PositionState(symbol=symbol.upper(), has_position=False, note="smoke",
                             sources=[PositionSource(source="ALPACA_PAPER_VIEW", label="Alpaca Paper View", status="NOT_REFRESHED", message="smoke")])

    def invalidate(self, symbol=None):
        pass


class _NoResearch:
    def latest(self, symbol):
        from trader_view.models import EvidenceSummary
        return EvidenceSummary(symbol=symbol.upper(), status="NOT_LOADED", data_status="NOT_LOADED", message="smoke", disclaimer="smoke")


def _forced_inputs(now: datetime, label: str, **over):
    from setups.models import SetupInputs
    iso = now.isoformat(timespec="seconds")
    base = dict(stretch_status="AVAILABLE", ppe_status="AVAILABLE", ppe_coverage=1.0, ppe_computed_at=iso, confidence_status="AVAILABLE", demand_status="AVAILABLE",
                demand_coverage=0.85, demand_mode="INTRADAY_MODE", demand_computed_at=iso, evidence_available=True, evidence_packet_id="5" * 32,
                evidence_created_at="2026-10-10T00:00:00+00:00", evidence_age_days=12, evidence_cause="MIXED", evidence_strength="WEAK", company_confirm="PARTIAL",
                quote_ts=int(now.timestamp()), quote_status="OK", market_data_source=label, mode="test", session_hint="REGULAR")
    base.update(over)
    return SetupInputs(symbol=SYMBOL, evaluated_at=iso, **base)


def build_stack(ctx: Ctx, db: Path):
    """The real Stage 5.8 / 5.9 / 5.9.1 / 6.0 services on the smoke's own scratch database (never the normal bot database)."""
    from execution.service import ExecutionService
    from execution.store import ExecutionStore
    from proposals.account import AccountAdapter
    from proposals.service import ProposalService
    from proposals.store import ProposalStore
    from safety.service import ExecutionSafetyService
    from safety.store import SafetyStore
    from setups.service import SetupService
    from setups.store import SetupStore
    from trader_view.stream import TerminalStream
    holder: Dict[str, Any] = {}
    svc = SetupService(stream_fn=lambda: holder["s"], store=SetupStore(db), clock=ctx.clock)
    safety = ExecutionSafetyService(stream_fn=lambda: holder["s"], store=SafetyStore(db), clock=ctx.clock, accept_unadjusted=not ctx.simulated)   # smoke-scoped exception
    psvc = ProposalService(stream_fn=lambda: holder["s"], store=ProposalStore(db), account=AccountAdapter(path=db), clock=ctx.clock, safety=safety)
    x = ExecutionService(stream_fn=lambda: holder["s"], store=ExecutionStore(db), broker=ctx.broker, clock=ctx.clock, owner=f"aapl-smoke:{os.getpid()}")
    x.qty_cap = MAX_QTY
    x.max_entries_per_cycle = 1
    s = TerminalStream(ctx.market, _NoPositions(), _NoResearch(), watchlist_fn=lambda: [], run_loop=False, setup_service=svc, proposal_service=psvc, safety_service=safety,
                       execution_service=x)
    holder["s"] = s
    return s


# ---------------------------------------------------------------- idempotency -----------------------------------------------------------
class SmokeLock:
    def __init__(self, path: Path):
        self.path, self.fh = path, None

    def acquire(self) -> bool:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.fh = open(self.path, "a+")
        try:
            import msvcrt
            self.fh.seek(0)
            msvcrt.locking(self.fh.fileno(), msvcrt.LK_NBLCK, 1)
            return True
        except ImportError:
            import fcntl
            try:
                fcntl.flock(self.fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                return True
            except OSError:
                return False
        except OSError:
            return False

    def release(self):
        if self.fh is None:
            return
        try:
            import msvcrt
            self.fh.seek(0)
            msvcrt.locking(self.fh.fileno(), msvcrt.LK_UNLCK, 1)
        except Exception:  # noqa: BLE001
            pass
        self.fh.close()
        self.fh = None


def _read_json(p: Path) -> Optional[dict]:
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _write_json(p: Path, data: dict) -> None:
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=1, default=str), encoding="utf-8")
    os.replace(tmp, p)


def refusal_reason(state: Optional[dict], run_date: str) -> Optional[str]:
    """Why another smoke must NOT run (None = may run). RUNNING always refuses (a crashed run is never repeated blindly)."""
    if not state:
        return None
    st = state.get("state")
    if st == "RUNNING":
        return f"a smoke run {state.get('run_id')} is RUNNING or ended without a final state (started {state.get('started_at')}); never repeated automatically"
    if state.get("run_date") == run_date and (st == "PASS" or (st == "FAIL" and int(state.get("broker_submissions") or 0) > 0)):
        return f"the {run_date} smoke already finished {st} (run {state.get('run_id')}); no second smoke"
    return None


# ---------------------------------------------------------------- the smoke ----------------------------------------------------------------
def run(ctx: Ctx) -> int:
    runtime, logs = Path(ctx.runtime_dir), Path(ctx.log_dir)
    runtime.mkdir(parents=True, exist_ok=True)
    logs.mkdir(parents=True, exist_ok=True)
    redact = Redactor(ctx.secrets)
    handler = logging.FileHandler(logs / LOG_FILE, encoding="utf-8")
    handler.setFormatter(_RedactFormatter(redact))
    LOG.addHandler(handler)
    LOG.setLevel(logging.INFO)
    lock = SmokeLock(runtime / LOCK_FILE)
    state_p, result_p, db = runtime / STATE_FILE, runtime / RESULT_FILE, runtime / DB_FILE
    run_id = uuid.uuid4().hex[:12]
    try:
        LOG.info("=== AAPL one-share Alpaca PAPER smoke, run %s, run date %s, mode %s, pid %s ===", run_id, ctx.run_date, "SIMULATED" if ctx.simulated else "ALPACA PAPER",
                 os.getpid())
        LOG.info("credentials in this process: %s (values never logged); no Robinhood, model or other .env value was loaded", ctx.loaded_names or "none")
        if not lock.acquire():
            LOG.error("REFUSED: another smoke process holds %s - no second smoke", lock.path)
            return EXIT_REFUSED
        why = refusal_reason(_read_json(state_p), ctx.run_date)
        if why:
            LOG.error("REFUSED: %s", why)
            return EXIT_REFUSED
        return _run_locked(ctx, run_id, state_p, result_p, db)
    finally:
        lock.release()
        LOG.removeHandler(handler)
        handler.close()


def _run_locked(ctx: Ctx, run_id: str, state_p: Path, result_p: Path, db: Path) -> int:
    from execution.store import ExecutionStore
    if db.exists():                                                           # a fresh scratch database per run; history is archived, never reused
        old = ExecutionStore(db)
        if old.exists() and old.intents():
            LOG.error("REFUSED: %s already holds execution intents - no second smoke on it", db)
            return EXIT_REFUSED
        db.rename(db.with_name(f"{db.stem}.{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}.archived{db.suffix}"))
    started = ctx.clock()
    _write_json(state_p, {"state": "RUNNING", "run_id": run_id, "run_date": ctx.run_date, "pid": os.getpid(), "started_at": started.isoformat(timespec="seconds"),
                          "broker_submissions": 0})
    R: Dict[str, Any] = {"run_id": run_id, "run_date": ctx.run_date, "mode": "simulated" if ctx.simulated else "alpaca_paper", "started_at": started.isoformat(timespec="seconds"),
                         "finished_at": None, "symbol": SYMBOL, "labels": [LABEL_SETUP, LABEL_EXIT] + ([] if ctx.simulated else [PNL_EXCEPTION]),
                         "paper_account_proven": False, "market_open": None, "safety_state": None, "reconciliation_before": None, "preflight": [],
                         "proposal_id": None, "proposal_qty": None, "execution_id": None, "entry_client_order_id": None, "entry_broker_order_id": None, "entry_posts": 0,
                         "entry_filled_qty": 0, "entry_avg_price": None, "position_id": None, "exit_reason": None, "exit_client_order_id": None, "exit_broker_order_id": None,
                         "exit_posts": 0, "exit_filled_qty": 0, "exit_avg_price": None, "realized_pnl": None, "broker_final_qty": None, "internal_final_state": None,
                         "reconciliation_after": None, "duplicate_orders": None, "orders_sent": [], "paper_auto_final": None, "secret_matches": None, "result": "FAIL",
                         "failure_reason": None}
    holder: Dict[str, Any] = {}
    code = EXIT_FAIL
    try:
        code = _smoke(ctx, R, db, holder)
    except NoOrder as exc:
        R["failure_reason"] = f"NO ORDER: {exc}"
        code = EXIT_NO_ORDER
    except SmokeFail as exc:
        R["failure_reason"] = str(exc)
        code = EXIT_FAIL
    except BaseException as exc:  # noqa: BLE001 - incl. KeyboardInterrupt: Paper Auto still goes off below
        R["failure_reason"] = f"{type(exc).__name__}: {exc}"
        LOG.error("unexpected failure:\n%s", traceback.format_exc())
        code = EXIT_FAIL
    finally:
        s = holder.get("s")
        guard = getattr(s, "_smoke_guard", None) if s is not None else None
        if s is not None:
            try:
                out = s.execution.disable_auto(actor="aapl-smoke", reason="smoke finished (always off in finally)")
                R["paper_auto_final"] = "OFF" if out["mode"] == "MONITOR_ONLY" else out["mode"]
            except BaseException as exc:  # noqa: BLE001
                R["paper_auto_final"] = f"UNKNOWN ({type(exc).__name__})"
                LOG.error("Paper Auto could not be switched off: %s", exc)
        else:
            R["paper_auto_final"] = "OFF (never enabled)"
        LOG.info("Paper Auto final: %s", R["paper_auto_final"])
        if guard is not None:
            R["orders_sent"] = list(guard.sent)
            R["entry_posts"], R["exit_posts"] = guard.count("buy"), guard.count("sell")
        if code == EXIT_PASS and R["paper_auto_final"] != "OFF":
            code, R["failure_reason"] = EXIT_FAIL, f"Paper Auto final state {R['paper_auto_final']}"
        R["finished_at"] = ctx.clock().isoformat(timespec="seconds")
        R["result"] = "PASS" if code == EXIT_PASS else "FAIL"
        _write_json(result_p, R)
        matches = _secret_scan(ctx, result_p)
        R["secret_matches"] = matches
        if matches and code == EXIT_PASS:
            code, R["result"], R["failure_reason"] = EXIT_FAIL, "FAIL", f"{matches} secret value match(es) in the smoke files"
        _write_json(result_p, R)
        subs = len(R["orders_sent"])
        final_state = "PASS" if code == EXIT_PASS else ("FAIL" if subs > 0 else "ABORTED_NO_ORDER")
        _write_json(state_p, {"state": final_state, "run_id": run_id, "run_date": ctx.run_date, "pid": os.getpid(), "started_at": R["started_at"], "finished_at": R["finished_at"],
                              "broker_submissions": subs, "result": R["result"], "failure_reason": R["failure_reason"]})
        LOG.info("RESULT %s%s | orders sent %s | result file %s", R["result"], f" - {R['failure_reason']}" if R["failure_reason"] else "",
                 [f"{o['side'].upper()} {o['qty']} {o['symbol']}" for o in R["orders_sent"]], result_p)
    return code


def _secret_scan(ctx: Ctx, result_p: Path) -> int:
    files = [Path(ctx.log_dir) / LOG_FILE, result_p] + [p for p in Path(ctx.log_dir).glob("*.log")]
    text = ""
    for p in dict.fromkeys(files):
        try:
            text += p.read_text(encoding="utf-8", errors="replace")
        except OSError:
            pass
    return sum(text.count(v) for v in ctx.secrets if isinstance(v, str) and len(v) >= 6)


def _need(R: dict, name: str, ok: bool, detail: str) -> None:
    R["preflight"].append({"check": name, "ok": bool(ok), "detail": detail})
    LOG.info("PREFLIGHT %s %s: %s", "OK  " if ok else "FAIL", name, detail)
    if not ok:
        raise NoOrder(f"{name}: {detail}")


def _cycle(s, ctx: Ctx, tag: str) -> dict:
    out = s.execution.run_cycle(ctx.clock(), "SMOKE")
    sm = out.get("summary") or {}
    LOG.info("executor cycle %s: %s %s%s", tag, out.get("result"), {k: sm.get(k) for k in ("monitored", "findings", "positions", "exits_decided", "exits_submitted", "safety", "entries")
                                                                    if k in sm}, f" error {out.get('error')}" if out.get("error") else "")
    g = s._smoke_guard
    if g.violation:
        raise SmokeFail(f"order guard violation: {g.violation} (refused before the wire)")
    return out


def _smoke(ctx: Ctx, R: dict, db: Path, holder: Dict[str, Any]) -> int:
    from execution.engine import D
    # ---- window ------------------------------------------------------------------------------------------------------------------------
    now = ctx.clock()
    et = et_now(now)
    lo, hi = WINDOW_ET
    in_window = et.date().isoformat() == ctx.run_date and lo <= et.strftime("%H:%M") < hi
    _need(R, "time window", in_window, f"New York {et.strftime('%Y-%m-%d %H:%M:%S')} (allowed {ctx.run_date} {lo}-{hi} ET; no catch-up)")
    # ---- server ------------------------------------------------------------------------------------------------------------------------
    srv = ctx.server_check()
    _need(R, "server reachable", bool(srv.get("ok")), srv.get("detail") or "")
    _need(R, "server bot in Monitor only", srv.get("mode") == "MONITOR_ONLY", f"server execution mode {srv.get('mode')} (the normal bot must not trade during the smoke)")
    # ---- stack ---------------------------------------------------------------------------------------------------------------------------
    s = build_stack(ctx, db)
    s._smoke_guard = OrderGuard(ctx.broker)
    holder["s"] = s
    x = s.execution
    if "after_build" in ctx.hooks:
        ctx.hooks["after_build"](s)
    if not ctx.simulated:
        LOG.warning("%s: this smoke process accepts unadjusted daily P&L from the Alpaca snapshot (deposits / withdrawals cannot be separated). "
                    "Scoped to this process only; permanent configuration unchanged; unattended safety is NOT solved by this.", PNL_EXCEPTION)
    # ---- pre-flight (fresh) -------------------------------------------------------------------------------------------------------------
    if ctx.refresh_snapshot is not None:
        snap = ctx.refresh_snapshot()
        conn = (snap.get("connection") or {}).get("state")
        _need(R, "paper snapshot refreshed", conn == "CONNECTED", f"4.6A snapshot {conn} at {snap.get('refreshed_at')}")
    env = ctx.broker.environment(ctx.clock())
    R["paper_account_proven"] = bool(env.get("paper"))
    _need(R, "1 paper account reachable", env.get("error") is None, f"account read {'ok' if env.get('error') is None else env.get('error')}")
    _need(R, "2 paper environment proven", bool(env.get("paper")) and env.get("host") == PAPER_HOST, f"{env.get('label')} {env.get('host')} {env.get('account_number_masked')}")
    _need(R, "3 account active", str(env.get("status") or "").upper() == "ACTIVE", f"status {env.get('status')}")
    _need(R, "4 account not blocked", not env.get("blocked"), f"blocked {env.get('blocked')}")
    acct = ctx.broker.account()
    eq, bp = D(acct.get("equity")), D(acct.get("buying_power"))
    _need(R, "5 equity available", eq is not None and eq > 0, f"equity {acct.get('equity')}")
    _need(R, "6 buying power available", bp is not None and bp > 0, f"buying power {acct.get('buying_power')}")
    clk = ctx.broker.clock()
    R["market_open"] = bool(clk.get("is_open"))
    _need(R, "7 broker clock open", bool(clk.get("is_open")), f"broker clock is_open {clk.get('is_open')} (next close {clk.get('next_close')})")
    asset = ctx.broker.asset(SYMBOL)
    _need(R, "8 AAPL tradable", bool(asset and asset.get("tradable") and str(asset.get("status")).lower() == "active"), f"asset {asset}")
    q = ctx.market.quote(SYMBOL)
    age = (now.timestamp() - float(q.ts)) if q.ts else None
    fresh = q.status == "OK" and q.price is not None and q.price > 0 and (ctx.simulated or (age is not None and age <= QUOTE_MAX_AGE_S))
    _need(R, "9 AAPL market data fresh", fresh, f"quote {q.status} {q.price} age {None if age is None else round(age, 1)} s (max {QUOTE_MAX_AGE_S:.0f} s) from {q.source}")
    if bp is not None and q.price:
        _need(R, "6b buying power covers one share", bp >= D(q.price) * Decimal("1.05"), f"{acct.get('buying_power')} vs {q.price} x 1.05")
    x.startup(ctx.clock())
    x._reconcile(ctx.clock(), "SMOKE", None)
    R["reconciliation_before"] = "HEALTHY" if x.recon_healthy else "BLOCKED: " + "; ".join(x.recon_reasons)
    _need(R, "10 reconciliation healthy", x.recon_healthy, R["reconciliation_before"] + f" (informational: {[(f['kind'], f['symbol']) for f in x.store.findings() if not f['blocking']]})")
    sv = s.safety.evaluate("SCHEDULED", ctx.clock())
    R["safety_state"] = sv.state
    _need(R, "11 safety service readable", sv.state != "UNKNOWN", f"{sv.state} ({sv.state_label}) evaluated {sv.evaluated_at}")
    _need(R, "11b safety allows new entries", sv.state == "TRADING_ALLOWED" and sv.allowed_new_entry, f"{sv.state_label}; reasons {sv.reason_codes}")
    oo = ctx.broker.orders("open", [SYMBOL])
    _need(R, "12 no conflicting AAPL broker order", not oo, f"{len(oo)} open AAPL order(s)")
    bpos = ctx.broker.position(SYMBOL)
    _need(R, "13 no unexpected AAPL broker position", bpos is None, f"broker AAPL qty {bpos['qty'] if bpos else 0}")
    today = et.date().isoformat()
    earlier = [o for o in ctx.broker.orders("all", [SYMBOL]) if str(o.get("client_order_id") or "").startswith("stockagent-")
               and str(o.get("submitted_at") or "")[:10] >= (et.date() - timedelta(days=1)).isoformat()]
    _need(R, "13b no earlier Stage 6.0 AAPL order (broker-side idempotency)", not earlier, f"{len(earlier)} stockagent AAPL order(s) since {today} - 1 day")
    _need(R, "14 share cap is 1 in this process", x.qty_cap == MAX_QTY, f"qty cap {x.qty_cap}")
    m = x.mode()
    fresh_db = not s.setups.store.open_episodes() and not x.store.intents()
    _need(R, "15 Paper Auto controlled by this smoke", m["mode"] == "MONITOR_ONLY" and fresh_db, f"smoke database {db.name}: mode {m['mode']}, fresh {fresh_db}")
    # ---- SMOKE_FORCED_SETUP -----------------------------------------------------------------------------------------------------------
    LOG.info("%s: forcing AAPL WATCH -> READY through the real Stage 5.8 setup service (infrastructure proof only; NOT a strategy discovery)", LABEL_SETUP)
    v1 = s.setups.evaluate_symbol(SYMBOL, ctx.clock(), "MANUAL", inputs=_forced_inputs(ctx.clock(), LABEL_SETUP, **WATCH))
    ctx.sleep(1.1)
    v2 = s.setups.evaluate_symbol(SYMBOL, ctx.clock(), "MANUAL", inputs=_forced_inputs(ctx.clock(), LABEL_SETUP, **READY))
    _need(R, "forced setup READY", (v1.state, v2.state) == ("WATCH", "READY"), f"{v1.state} -> {v2.state} (setup {v2.setup_id}, {LABEL_SETUP})")
    pv = s.proposals.evaluate(SYMBOL, ctx.clock(), "MANUAL")
    R["proposal_id"], R["proposal_qty"] = pv.proposal_id, pv.final_qty
    _need(R, "proposal REVIEWABLE and eligible", pv.effective_status == "REVIEWABLE" and bool(pv.execution_eligibility.get("allowed")),
          f"{pv.effective_status} ref {pv.reference_price} ({pv.price_source}) invalidation {pv.invalidation_price} qty {pv.final_qty} -> capped to {MAX_QTY}; "
          f"blockers {[b['code'] for b in pv.blockers]} warnings {[w['code'] for w in pv.warnings]}")
    if ctx.sim is not None:
        ctx.sim.set_price(SYMBOL, float(pv.reference_price))
    # ---- Paper Auto on (the real execution service) ----------------------------------------------------------------------------------
    if ctx.refresh_snapshot is not None:
        ctx.refresh_snapshot()                                                   # the gate needs a fresh equity read at the entry
    out = x.enable_auto(actor="aapl-smoke", reason="supervised one-share AAPL paper smoke", now=ctx.clock())
    _need(R, "Paper Auto enabled by this smoke", out["mode"] == "PAPER_AUTO", f"mode {out['mode']}")
    # ---- last look before the entry --------------------------------------------------------------------------------------------------
    sv = s.safety.current(now=ctx.clock())
    pv2 = s.proposals.current(SYMBOL, ctx.clock())
    _need(R, "pre-submit: paper environment", bool(x.environment_cache.get("paper")), f"{x.environment_cache.get('label')}")
    _need(R, "pre-submit: safety", sv.state == "TRADING_ALLOWED" and sv.allowed_new_entry, sv.state_label)
    _need(R, "pre-submit: proposal fresh", pv2.proposal_id == pv.proposal_id and pv2.effective_status == "REVIEWABLE", f"{pv2.effective_status} expires {pv2.expires_at}")
    _need(R, "pre-submit: reconciliation", x.recon_healthy, "healthy" if x.recon_healthy else "; ".join(x.recon_reasons))
    _need(R, "pre-submit: order count and quantity", not s._smoke_guard.sent and x.qty_cap == MAX_QTY, f"orders sent {len(s._smoke_guard.sent)}, cap {x.qty_cap}")
    # ---- entry -----------------------------------------------------------------------------------------------------------------------------
    c = _cycle(s, ctx, "entry")
    ents = x.store.intents(intent_type="ENTRY")
    if len(ents) != 1:
        raise SmokeFail(f"expected exactly one entry intent, found {len(ents)} (entries {(c.get('summary') or {}).get('entries')})")
    it = ents[0]
    R["execution_id"], R["entry_client_order_id"] = it.execution_id, it.client_order_id
    if s._smoke_guard.count("buy") != 1:
        raise SmokeFail(f"entry not sent ({it.state} {it.failure_code}: {it.failure_text})")
    if "after_entry_post" in ctx.hooks:
        ctx.hooks["after_entry_post"](s)
    if it.state == "RECONCILIATION_REQUIRED":
        LOG.warning("ambiguous BUY result (%s %s): reconciling by client_order_id %s only - never resent", it.outcome_class, it.failure_code, it.client_order_id)
        t0 = ctx.clock()
        while (ctx.clock() - t0).total_seconds() < AMBIGUOUS_TIMEOUT_S:
            ctx.sleep(POLL_S)
            _cycle(s, ctx, "entry reconciliation")
            it = x.store.intent(it.execution_id)
            if it.state != "RECONCILIATION_REQUIRED":
                break
        if it.state in ("RECONCILIATION_REQUIRED", "NOT_SENT", "FAILED"):
            raise SmokeFail(f"ambiguous BUY unresolved ({it.state} {it.failure_code}); no second BUY")
        LOG.info("ambiguous BUY resolved by client_order_id: %s broker order %s", it.state, it.broker_order_id)
    if it.state in ("REJECTED", "CANCELED", "NOT_SENT", "FAILED"):
        raise SmokeFail(f"entry {it.state} ({it.failure_code}: {it.failure_text}); no retry")
    t0 = ctx.clock()
    while (ctx.clock() - t0).total_seconds() < FILL_TIMEOUT_S:
        it = x.store.intent(it.execution_id)
        if it.state == "FILLED" and x.store.open_position(SYMBOL) is not None:
            break
        if it.state in ("REJECTED", "CANCELED", "NOT_SENT", "FAILED"):
            raise SmokeFail(f"entry {it.state} ({it.failure_code}); no retry")
        ctx.sleep(POLL_S)
        _cycle(s, ctx, "entry fill")
    it = x.store.intent(it.execution_id)
    pos = x.store.open_position(SYMBOL)
    R["entry_broker_order_id"], R["entry_filled_qty"], R["entry_avg_price"] = it.broker_order_id, it.filled_qty, it.filled_avg_price
    if "after_entry_fill" in ctx.hooks:
        ctx.hooks["after_entry_fill"](s)
    x._reconcile(ctx.clock(), "SMOKE", None)
    checks = [("entry intent FILLED", it.state == "FILLED"), ("BUY filled qty == 1", it.filled_qty == MAX_QTY), ("one entry intent", len(x.store.intents(intent_type="ENTRY")) == 1),
              ("one BUY sent", s._smoke_guard.count("buy") == 1), ("internal position qty == 1", pos is not None and pos.quantity == MAX_QTY),
              ("reconciliation healthy after the entry", x.recon_healthy)]
    for name, ok in checks:
        LOG.info("ENTRY %s %s", "OK  " if ok else "FAIL", name)
    bad = [n for n, ok in checks if not ok]
    if bad:
        raise SmokeFail(f"entry not confirmed ({', '.join(bad)}; intent {it.state} filled {it.filled_qty}; reconciliation {x.recon_reasons}); no SELL is sent blindly")
    R["position_id"] = pos.position_id
    # ---- SMOKE_FORCED_EXIT ------------------------------------------------------------------------------------------------------------
    LOG.info("%s: forcing the AAPL setup's stretch to normalize through the real setup service; the executor decides the exit", LABEL_EXIT)
    v3 = s.setups.evaluate_symbol(SYMBOL, ctx.clock(), "MANUAL", inputs=_forced_inputs(ctx.clock(), LABEL_EXIT, **NORMALIZED))
    if v3.state != "INVALIDATED":
        raise SmokeFail(f"forced exit did not invalidate the setup ({v3.state} {v3.reason_code})")
    ctx.sleep(POLL_S)
    c = _cycle(s, ctx, "exit")
    exits = x.store.intents(intent_type="EXIT")
    p = x.store.position(pos.position_id)
    R["exit_reason"] = p.exit_reason
    if len(exits) != 1 or s._smoke_guard.count("sell") != 1:
        raise SmokeFail(f"expected one exit intent and one SELL, found {len(exits)} intent(s) and {s._smoke_guard.count('sell')} SELL(s) (exit reason {p.exit_reason}, "
                        f"position {p.state})")
    ex = exits[0]
    R["exit_client_order_id"] = ex.client_order_id
    if "after_exit_post" in ctx.hooks:
        ctx.hooks["after_exit_post"](s)
    t0 = ctx.clock()
    while (ctx.clock() - t0).total_seconds() < max(FILL_TIMEOUT_S, AMBIGUOUS_TIMEOUT_S):
        ex = x.store.intent(ex.execution_id)
        p = x.store.position(pos.position_id)
        if p.state == "CLOSED":
            break
        if ex.state in ("REJECTED", "CANCELED", "NOT_SENT", "FAILED"):
            raise SmokeFail(f"exit {ex.state} ({ex.failure_code}); no further SELL - the 1-share AAPL paper position may still be open")
        ctx.sleep(POLL_S)
        _cycle(s, ctx, "exit fill" if ex.state != "RECONCILIATION_REQUIRED" else "exit reconciliation")
    ex = x.store.intent(ex.execution_id)
    p = x.store.position(pos.position_id)
    R["exit_broker_order_id"], R["exit_filled_qty"], R["exit_avg_price"], R["realized_pnl"] = ex.broker_order_id, ex.filled_qty, ex.filled_avg_price, p.realized_pnl
    # ---- final reconciliation -------------------------------------------------------------------------------------------------------
    x._reconcile(ctx.clock(), "SMOKE", None)
    bfinal = ctx.broker.position(SYMBOL)
    R["broker_final_qty"] = int(bfinal["qty"]) if bfinal else 0
    R["internal_final_state"] = p.state
    R["reconciliation_after"] = "HEALTHY" if x.recon_healthy else "BLOCKED: " + "; ".join(x.recon_reasons)
    ours = {i.client_order_id for i in x.store.intents()}
    broker_ours = [o for o in ctx.broker.orders("all", [SYMBOL]) if str(o.get("client_order_id") or "").startswith("stockagent-")]
    R["duplicate_orders"] = max(0, len(broker_ours) - 2) + sum(1 for o in broker_ours if o.get("client_order_id") not in ours)
    final = [("entry intent count == 1", len(x.store.intents(intent_type="ENTRY")) == 1), ("BUY POST count == 1", s._smoke_guard.count("buy") == 1),
             ("BUY filled qty == 1", it.filled_qty == MAX_QTY), ("internal position created once", len(x.store.positions(open_only=False)) == 1),
             ("exit intent count == 1", len(x.store.intents(intent_type="EXIT")) == 1), ("SELL POST count == 1", s._smoke_guard.count("sell") == 1),
             ("SELL filled qty == 1", ex.filled_qty == MAX_QTY), ("Alpaca AAPL position == 0", R["broker_final_qty"] == 0), ("internal position CLOSED", p.state == "CLOSED"),
             ("reconciliation healthy", x.recon_healthy), ("duplicate orders == 0", R["duplicate_orders"] == 0), ("two broker orders in total", len(s._smoke_guard.sent) == 2)]
    for name, ok in final:
        LOG.info("FINAL %s %s", "OK  " if ok else "FAIL", name)
    bad = [n for n, ok in final if not ok]
    if bad:
        raise SmokeFail("final reconciliation failed: " + ", ".join(bad))
    return EXIT_PASS


# ---------------------------------------------------------------- process entry ------------------------------------------------------------
def _server_check_http(url: str) -> Callable[[], Dict[str, Any]]:
    def check() -> Dict[str, Any]:
        import urllib.request
        try:
            with urllib.request.urlopen(url.rstrip("/") + "/api/health", timeout=10) as r:
                health = json.loads(r.read().decode("utf-8"))
            with urllib.request.urlopen(url.rstrip("/") + "/api/execution/status", timeout=20) as r:
                st = json.loads(r.read().decode("utf-8"))
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "detail": f"{url} not reachable ({type(exc).__name__})"}
        return {"ok": health.get("status") == "ok", "mode": st.get("mode"), "detail": f"{url} health {health.get('status')}, bot mode {st.get('mode')}"}
    return check


def parse(argv=None):
    ap = argparse.ArgumentParser(description="Stage 6.0 supervised one-share AAPL Alpaca PAPER smoke (standalone).")
    ap.add_argument("--app-root", required=True)
    ap.add_argument("--env-file", default="")
    ap.add_argument("--runtime-dir", default="")
    ap.add_argument("--log-dir", default="")
    ap.add_argument("--run-date", required=True, help="the New York date the smoke may run on (YYYY-MM-DD)")
    ap.add_argument("--server-url", default="http://127.0.0.1:8000")
    ap.add_argument("--simulated", action="store_true", help="simulated paper broker + simulated market data (offline proof; no Alpaca call)")
    ap.add_argument("--clock-et", default="", help="simulated only: the New York wall clock to start from, e.g. '2026-10-23 09:46'")
    return ap.parse_args(argv)


def main(argv=None) -> int:
    a = parse(argv)
    app_root = Path(a.app_root).resolve()
    runtime = Path(a.runtime_dir).resolve() if a.runtime_dir else app_root / "runtime"
    logs = Path(a.log_dir).resolve() if a.log_dir else app_root / "logs"
    if a.clock_et and not a.simulated:
        print("--clock-et is only allowed with --simulated", file=sys.stderr)
        return EXIT_NO_ORDER
    # credential isolation: only the four Alpaca names; nothing else from .env; config's own .env loader is disabled for this process
    creds = select_credentials(Path(a.env_file)) if a.env_file else {}
    for k in list(os.environ):
        if KEEP_OUT_PATTERN.search(k) or k in ALPACA_NAMES or k == "STOCK_AGENT_ENV":
            os.environ.pop(k, None)
    os.environ.update(creds)
    os.environ.update({"TRADER_DATA_MODE": "simulated" if a.simulated else "real", "EXECUTION_SCHEDULER_ENABLED": "false", "SETUP_EVALUATOR_ENABLED": "false",
                       "EVENT_WARMUP_ON_STARTUP": "false", "PORTFOLIO_AWARENESS_ENABLED": "false", "EXECUTION_MAX_SHARES_PER_ENTRY": str(MAX_QTY),
                       "EXECUTION_BROKER": "simulated" if a.simulated else "alpaca_paper"})
    if a.simulated:
        os.environ["TRADE_PROPOSAL_REVIEW_EQUITY"] = "100000"
    import dotenv
    dotenv.load_dotenv = lambda *args, **kw: False                         # config.py must not load any other .env into this process
    os.chdir(app_root)
    sys.path.insert(0, str(app_root))
    runtime.mkdir(parents=True, exist_ok=True)
    import database.database as dbm
    dbm._db_instance = dbm.ResearchDatabase(runtime / READS_DB_FILE)         # app-wide readers use a smoke-only file, never the bot / project database
    import config
    assert not config.ANTHROPIC_API_KEY and not any(KEEP_OUT_PATTERN.search(k) for k in os.environ if k not in ALPACA_NAMES), "credential isolation failed"
    assert config.EXECUTION_MAX_SHARES_PER_ENTRY == MAX_QTY
    console = logging.StreamHandler(sys.stdout)
    redact = Redactor(list(creds.values()))
    console.setFormatter(_RedactFormatter(redact))
    root = logging.getLogger()
    root.addHandler(console)
    root.setLevel(logging.WARNING)
    LOG.addHandler(console)
    LOG.propagate = False
    logs.mkdir(parents=True, exist_ok=True)
    wfile = logging.FileHandler(logs / LOG_FILE, encoding="utf-8")
    wfile.setFormatter(_RedactFormatter(redact))
    wfile.setLevel(logging.WARNING)
    root.addHandler(wfile)                                                  # library warnings / errors land in the smoke log too
    from execution import broker as BK
    if a.simulated:
        from execution.simulated import SimulatedPaperBroker
        from trader_view.simulated import SimulatedMarket
        from zoneinfo import ZoneInfo
        base = datetime.strptime(a.clock_et, "%Y-%m-%d %H:%M").replace(tzinfo=ZoneInfo("America/New_York")).astimezone(timezone.utc) if a.clock_et else datetime.now(timezone.utc)
        off = {"s": 0.0}
        clock = lambda: base + timedelta(seconds=off["s"])  # noqa: E731
        sim = SimulatedPaperBroker(now_fn=clock)
        broker = BK.AlpacaPaperBroker(simulated=True, wire=lambda: sim)
        market = SimulatedMarket(seed=11, start_time=int(base.timestamp()))
        ctx = Ctx(broker=broker, market=market, clock=clock, sleep=lambda sec: off.__setitem__("s", off["s"] + sec), server_check=_server_check_http(a.server_url),
                  refresh_snapshot=None, simulated=True, runtime_dir=runtime, log_dir=logs, run_date=a.run_date, secrets=list(creds.values()), sim=sim,
                  loaded_names=sorted(creds))
    else:
        from paper import alpaca_view as AV
        from trader_view.market_real import RealMarketData
        ctx = Ctx(broker=BK.AlpacaPaperBroker(), market=RealMarketData(), clock=lambda: datetime.now(timezone.utc), sleep=time.sleep,
                  server_check=_server_check_http(a.server_url), refresh_snapshot=AV.refresh, simulated=False, runtime_dir=runtime, log_dir=logs, run_date=a.run_date,
                  secrets=list(creds.values()), loaded_names=sorted(creds))
    return run(ctx)


if __name__ == "__main__":
    sys.exit(main())
