"""
Wave 2 PR B test support (WAVE2_PRB_TEST_PLAN_REV3_4.md): synthetic data only, tmp_path databases, no network.

  et()                  a New York wall-clock time as a UTC datetime
  migrate()             the evidence tables of a production database (W-01: the executor tables exist, as executor startup makes them)
  worker_status()       patches execution.scheduler.status so a sample persists loop_running / worker_stale_after_s (V-23, W-05 flags)
  healthy_sample()      the fields of a healthy observer sample; bulk_samples() inserts many, time-ordered, in one transaction
  cover_until()         the 6.1 rig's time-ordered minute-cadence in-window cover (design 8.4), with the variant's flags (W-05)
  Lab                   a lightweight observer: ForwardValidationService over a stub stream on its own database (O-level tests)
  row writers           execution / refresh / event rows with an exact event time (A-level evidence)
"""
from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, Iterable, List, Optional, Tuple
from zoneinfo import ZoneInfo

UTC = timezone.utc
NY = ZoneInfo("America/New_York")


def et(y: int, m: int, d: int, hh: int = 0, mm: int = 0, ss: int = 0) -> datetime:
    return datetime(y, m, d, hh, mm, ss, tzinfo=NY).astimezone(UTC)


def iso(dt: datetime) -> str:
    return dt.astimezone(UTC).isoformat(timespec="seconds")


def migrate(db: Path) -> None:
    """Every table the classifier v2 reads, as a production database has them (executor, safety, refresh, cash flow, autorun, observer)."""
    from database.account_refresh_migrations import run_account_refresh_migrations
    from database.autorun_migrations import run_autorun_migrations
    from database.cash_flow_migrations import run_cash_flow_migrations
    from database.execution_migrations import run_execution_migrations
    from database.execution_safety_migrations import run_execution_safety_migrations
    from database.forward_validation_migrations import run_forward_validation_migrations
    conn = sqlite3.connect(str(db))
    try:
        for fn in (run_execution_migrations, run_execution_safety_migrations, run_account_refresh_migrations, run_cash_flow_migrations, run_autorun_migrations,
                   run_forward_validation_migrations):
            fn(conn)
        conn.commit()
    finally:
        conn.close()


def worker_status(monkeypatch, *, running: bool = True, interval_s: float = 30.0, idle_interval_s: float = 300.0) -> None:
    """The executor scheduler's status as the observer's _snapshot reads it: `running` -> loop_running; activity.worker() turns the interval
    into stale_after_s = 2 * min(interval, 300) + 30 (90 s at 30 s). In a rig the real status says nothing (V-23)."""
    import execution.scheduler as ES
    status = {"running": running, "interval_s": interval_s, "idle_interval_s": idle_interval_s, "max_sleep_s": 300.0, "market": {"regular_session": True},
              "heartbeat_at": None, "last_result": {}, "in_cycle": False}
    monkeypatch.setattr(ES, "status", lambda *a, **k: dict(status))


def healthy_sample(at: datetime, owner: str, **over) -> Dict[str, Any]:
    from setups.market_hours import is_regular_session, session_date
    s = {"at": iso(at), "trading_date": (session_date(at) or at.date()).isoformat(), "in_session": is_regular_session(at), "owner": owner, "mode": "MONITOR_ONLY",
         "executor_ready": True, "safety_state": "TRADING_ALLOWED", "allowed_new_entry": True, "safety_codes": [], "reconciliation_healthy": True, "attention_count": 0,
         "attention": [], "account_status": "FRESH", "account_age_s": 5.0, "account_failures": 0, "equity": None, "cash": None, "buying_power": None, "net_external_cum": None,
         "cash_flow_status": "FRESH", "cash_flow_age_s": 5.0, "cash_flow_failures": 0, "open_positions": 0, "pending_entries": 0, "pending_exits": 0, "positions": [],
         "controls": {}, "last_cycle_at": iso(at - timedelta(seconds=30)), "last_cycle_result": "IDLE", "loop_running": True, "rr_intents": 0, "attention_positions": 0,
         "failed_intents": 0, "worker_stale_after_s": 90.0}
    s.update(over)
    return s


def bulk_samples(db: Path, samples: Iterable[Dict[str, Any]]) -> int:
    """Insert samples in the given (time) order in one transaction, exactly as ForwardValidationStore.add_sample writes one row."""
    from forward_validation.store import SAMPLE_COLS, SAMPLE_JSON, ForwardValidationStore
    ForwardValidationStore(db).ensure()
    rows = []
    for row in samples:
        d = {k: row.get(k) for k in SAMPLE_COLS}
        d["sample_id"] = row.get("sample_id") or uuid.uuid4().hex
        for col, key in SAMPLE_JSON.items():
            d[col] = json.dumps(row.get(key, [] if key != "controls" else {}), sort_keys=True, separators=(",", ":"), default=str)
        for k in ("in_session", "executor_ready"):
            d[k] = 1 if row.get(k) else 0
        for k in ("allowed_new_entry", "reconciliation_healthy", "loop_running"):
            d[k] = None if row.get(k) is None else (1 if row.get(k) else 0)
        for k in ("attention_count", "open_positions", "pending_entries", "pending_exits"):
            d[k] = int(row.get(k) or 0)
        rows.append([d[c] for c in SAMPLE_COLS])
    if not rows:
        return 0
    conn = sqlite3.connect(str(db))
    try:
        conn.executemany(f"INSERT INTO forward_validation_samples ({', '.join(SAMPLE_COLS)}) VALUES ({', '.join('?' * len(SAMPLE_COLS))})", rows)
        conn.commit()
    finally:
        conn.close()
    return len(rows)


def minutes(start: datetime, end: datetime, step_s: int = 60, *, inclusive: bool = True) -> List[datetime]:
    out, t = [], start
    while t < end or (inclusive and t == end):
        out.append(t)
        t += timedelta(seconds=step_s)
    return out


def cover_until(r, t: datetime, *, inclusive: bool = False, loop_running: bool = True, account_status: str = "FRESH", cash_flow_status: str = "FRESH") -> int:
    """The 6.1 rig's in-window cover (design 8.4): healthy minute-cadence samples of the rig's own owner from max(W.open, the last sample's
    at + 60 s) up to t (exclusive unless `inclusive`), clipped to the regular session, inserted in time order so no sample becomes a suspect.
    The variant's flags are kept (W-05). Returns the rows inserted (also added to r.cover_rows)."""
    from forward_capture import health as FH
    from setups.market_hours import session_date
    w = FH.window((session_date(t) or t.date()).isoformat())
    if w is None:
        return 0
    last = r.fv.store.last_sample()
    start = w[0]
    if last is not None:
        la = datetime.fromisoformat(last["at"])
        start = max(start, la + timedelta(seconds=60))
    end = min(t, w[1])
    times = [x for x in minutes(start, end, inclusive=inclusive or end < t) if x <= w[1] and (inclusive or x < t)]
    n = bulk_samples(r.db, (healthy_sample(x, r.fv.owner, loop_running=loop_running, account_status=account_status, cash_flow_status=cash_flow_status) for x in times))
    r.cover_rows = getattr(r, "cover_rows", 0) + n
    return n


# ================================================================ the lightweight observer lab =================================================
class _Exec:
    def __init__(self, store):
        self.store = store
        self.last_cycle: Optional[dict] = None
        self.ready = True
        self.recon_healthy = True
        self.recon_reasons: List[str] = []
        self.disable_calls = 0
        self.disable_raises = False

    def mode(self) -> dict:
        return self.store.mode()

    def needs_broker(self) -> bool:
        return False

    def disable_auto(self, actor: str, reason: str, now: datetime) -> dict:
        self.disable_calls += 1
        if self.disable_raises:
            raise RuntimeError("disable_auto failed (test)")
        return self.store.set_mode("MONITOR_ONLY", now, actor, reason, {})


class Lab:
    """ForwardValidationService over a stub stream on its own fully migrated database (W-01). `tick(at, ...)` runs one observe() at `at`, after
    the executor cycle (last_cycle) and the account-refresh attempt it is told to record (the settle conditions of B-17)."""

    def __init__(self, tmp_path: Path, monkeypatch, *, owner: str = "7001:aaaaaaaa", name: str = "w2b.db", running: bool = True, template: Optional[Path] = None):
        from execution.store import ExecutionStore
        from forward_validation.service import ForwardValidationService
        from forward_validation.store import ForwardValidationStore
        Path(tmp_path).mkdir(parents=True, exist_ok=True)
        self.db = Path(tmp_path) / name
        if template is not None:
            self.db.write_bytes(Path(template).read_bytes())                           # a migrated template (T-S25: one migration for all traces)
        else:
            migrate(self.db)
        self.monkeypatch = monkeypatch
        worker_status(monkeypatch, running=running)
        self.state = {"safety_state": "TRADING_ALLOWED", "allowed_new_entry": True, "account_status": "FRESH", "cash_flow_status": "FRESH"}
        self.x = _Exec(ExecutionStore(self.db))
        lab = self
        self.stream = SimpleNamespace(
            execution=self.x,
            safety=SimpleNamespace(current=lambda now=None: SimpleNamespace(state=lab.state["safety_state"], allowed_new_entry=lab.state["allowed_new_entry"], reason_codes=[])),
            account_refresh=SimpleNamespace(freshness=lambda now=None: {"status": lab.state["account_status"], "age_seconds": 5.0, "consecutive_failures": 0, "max_failures": 3},
                                            store=SimpleNamespace(snapshot=lambda: {"account": {"equity": "100000.00", "cash": "100000.00", "buying_power": "100000.00"}})),
            cash_flow=SimpleNamespace(freshness=lambda now=None: {"status": lab.state["cash_flow_status"], "age_seconds": 5.0, "consecutive_failures": 0, "max_failures": 3},
                                      adjustment=lambda *a, **k: {}))
        self.store = ForwardValidationStore(self.db)
        self.owner = owner
        self.fv = ForwardValidationService(lambda: self.stream, self.store, owner=owner, early_stop_enabled=True)

    def new_process(self, owner: str) -> None:
        """A restart: a new service (a new owner) on the same database and stream."""
        from forward_validation.service import ForwardValidationService
        self.owner = owner
        self.fv = ForwardValidationService(lambda: self.stream, self.store, owner=owner, early_stop_enabled=True)

    def tick(self, at: datetime, *, cycle: bool = True, refresh: bool = True, **state) -> dict:
        self.state.update(state)
        if cycle:
            self.x.last_cycle = {"at": iso(at - timedelta(seconds=10)), "finished_at": iso(at - timedelta(seconds=9)), "result": "IDLE"}
        if refresh:
            add_refresh(self.db, at - timedelta(seconds=5))
        return self.fv.observe(at, "TEST")

    def run(self, times: Iterable[datetime], **kw) -> List[dict]:
        return [self.tick(t, **kw) for t in times]

    def session(self, d: str) -> Optional[dict]:
        return self.store.session(d)

    def events(self, kind: str) -> List[dict]:
        return self.store.events(kind=kind, limit=10000, newest_first=False)

    def sql(self, sql: str, args=()) -> None:
        conn = sqlite3.connect(str(self.db))
        try:
            conn.execute(sql, args)
            conn.commit()
        finally:
            conn.close()


# ================================================================ evidence rows with an exact event time =======================================
def _exec_sql(db: Path, sql: str, args) -> None:
    conn = sqlite3.connect(str(db))
    try:
        conn.execute(sql, args)
        conn.commit()
    finally:
        conn.close()


def add_refresh(db: Path, started: datetime, *, result: str = "SUCCESS", error_code: Optional[str] = None, error_text: Optional[str] = None) -> None:
    _exec_sql(db, "INSERT INTO account_refresh_attempts (attempt_id, started_at, finished_at, trigger, result, read_attempts, error_code, error_text, refresh_id, owner) "
                  "VALUES (?,?,?,?,?,1,?,?,NULL,'test')", (uuid.uuid4().hex, iso(started), iso(started + timedelta(seconds=1)), "TEST", result, error_code, error_text))


def add_reconciliation(db: Path, at: datetime, kind: str, *, symbol: str = "SNDK", detail: Optional[dict] = None, blocking: int = 1) -> None:
    conn = sqlite3.connect(str(db))
    try:
        cols = [r[1] for r in conn.execute("PRAGMA table_info(execution_reconciliations)")]
        vals = {"at": iso(at), "kind": kind, "symbol": symbol, "detail_json": json.dumps(detail or {}), "blocking": blocking, "trigger": "CYCLE"}
        for c in cols:
            if c.endswith("_id") and c not in vals:
                vals[c] = uuid.uuid4().hex if c in ("finding_id", "reconciliation_id") else None
        use = [c for c in cols if c in vals]
        conn.execute(f"INSERT INTO execution_reconciliations ({', '.join(use)}) VALUES ({', '.join('?' * len(use))})", [vals[c] for c in use])
        conn.commit()
    finally:
        conn.close()


def add_event(db: Path, at: datetime, kind: str, code: Optional[str], detail: Optional[dict] = None, trading_date: Optional[str] = None, owner: str = "test") -> None:
    from forward_validation.store import ForwardValidationStore
    ForwardValidationStore(db).add_event(at, kind, code, detail or {}, trading_date=trading_date, owner=owner)


def add_startup(db: Path, at: datetime, pid: int, trading_date: Optional[str] = None) -> None:
    _exec_sql(db, "INSERT INTO autorun_events (event_id, at, trading_date, kind, code, actor, detail_json, owner) VALUES (?,?,?,?,?,?,?,?)",
              (uuid.uuid4().hex, iso(at), trading_date, "STARTUP", "MONITOR_ONLY", "autorun_policy", json.dumps({"pid": pid, "instance": f"{pid}:x"}), f"{pid}:x"))


def add_run(db: Path, started: datetime, *, result: str = "RUN", error: Optional[str] = None) -> None:
    conn = sqlite3.connect(str(db))
    try:
        cols = {r[1]: r for r in conn.execute("PRAGMA table_info(execution_runs)")}
        vals = {"run_id": uuid.uuid4().hex, "started_at": iso(started), "finished_at": iso(started + timedelta(seconds=1)), "trigger": "TEST", "mode": "MONITOR_ONLY",
                "result": result, "error": error, "summary_json": "{}"}
        use = [c for c in cols if c in vals]
        conn.execute(f"INSERT INTO execution_runs ({', '.join(use)}) VALUES ({', '.join('?' * len(use))})", [vals[c] for c in use])
        conn.commit()
    finally:
        conn.close()


def add_cash_refresh(db: Path, started: datetime, *, result: str = "SUCCESS", error_code: Optional[str] = None, error_text: Optional[str] = None) -> None:
    _exec_sql(db, "INSERT INTO cash_flow_refresh_attempts (attempt_id, started_at, finished_at, trigger, result, error_code, error_text) VALUES (?,?,?,?,?,?,?)",
              (uuid.uuid4().hex, iso(started), iso(started + timedelta(seconds=1)), "TEST", result, error_code, error_text))


def add_order(db: Path, coid: str, first_seen: datetime, *, broker_order_id: Optional[str] = None, symbol: str = "SNDK") -> None:
    _exec_sql(db, "INSERT INTO execution_orders (broker_order_id, client_order_id, symbol, side, qty, status, filled_qty, first_seen_at, last_seen_at, raw_json) "
                  "VALUES (?,?,?,?,?,?,0,?,?,'{}')", (broker_order_id or uuid.uuid4().hex, coid, symbol, "buy", "1", "accepted", iso(first_seen), iso(first_seen)))


def add_intent(db: Path, at: datetime, *, state: str = "SUBMITTED", intent_type: str = "ENTRY", submitted: Optional[datetime] = None, attempt_count: int = 1,
               updated: Optional[datetime] = None, symbol: str = "SNDK") -> str:
    eid = uuid.uuid4().hex
    _exec_sql(db, "INSERT INTO execution_intents (execution_id, intent_type, symbol, side, qty, client_order_id, state, attempt_count, created_at, submitted_at, updated_at) "
                  "VALUES (?,?,?,?,?,?,?,?,?,?,?)", (eid, intent_type, symbol, "buy", 1, f"stockagent-entry-{eid[:12]}", state, attempt_count, iso(at),
                                                     iso(submitted) if submitted else None, iso(updated or at)))
    return eid


def add_position(db: Path, at: datetime, *, state: str = "ATTENTION", symbol: str = "SNDK") -> str:
    pid = uuid.uuid4().hex
    _exec_sql(db, "INSERT INTO paper_positions (position_id, execution_id, symbol, quantity, entry_qty, average_entry_price, opened_at, state, attention_reason, updated_at) "
                  "VALUES (?,?,?,?,?,?,?,?,?,?)", (pid, uuid.uuid4().hex, symbol, 1, 1, "10.00", iso(at), state, "QTY_MISMATCH" if state == "ATTENTION" else None, iso(at)))
    return pid


def add_control(db: Path, control_id: str, updated: datetime, *, last_error: Optional[str] = None) -> None:
    _exec_sql(db, "INSERT INTO execution_controls (control_id, last_evaluated_at, evaluation_count, fire_count, last_result, last_input_json, last_error, updated_at) "
                  "VALUES (?,?,1,0,'PASS','{}',?,?) ON CONFLICT(control_id) DO UPDATE SET last_error = excluded.last_error, updated_at = excluded.updated_at",
              (control_id, iso(updated), last_error, iso(updated)))


def add_safety_eval(db: Path, at: datetime, *, state: str = "TRADING_ALLOWED", allowed: bool = True) -> None:
    _exec_sql(db, "INSERT INTO execution_safety_evaluations (evaluation_id, evaluated_at, trigger, state, allowed_new_entry, reason_codes_json, daily_loss_json, kill_json, "
                  "pause_json, freshness_json, input_json) VALUES (?,?,?,?,?,'[]','{}','{}','{}','{}','{}')", (uuid.uuid4().hex, iso(at), "TEST", state, 1 if allowed else 0))


def add_disarm(db: Path, at: datetime, trading_date: str, code: str = "SESSION_ENDED", codes: Optional[List[str]] = None) -> None:
    _exec_sql(db, "INSERT INTO autorun_events (event_id, at, trading_date, kind, code, actor, detail_json, owner) VALUES (?,?,?,?,?,?,?,?)",
              (uuid.uuid4().hex, iso(at), trading_date, "AUTO_DISARM", code, "autorun_policy", json.dumps({"codes": codes or [code]}), "x"))


def open_row(db: Path, d: str, at: datetime, owner: str = "test", *, in_session: bool = True) -> None:
    from forward_validation.store import ForwardValidationStore
    ForwardValidationStore(db).open_session(d, at, owner, in_session=in_session)


def assess(fv, d: str, trigger_at: datetime, *, late: bool = False, owner: Optional[str] = None, escalate: bool = False):
    """A-level: classify date `d` from the persisted evidence with a trigger sample at `trigger_at` (inserted when absent): (status, notes, assessment)."""
    from forward_validation.store import ForwardValidationStore
    st: ForwardValidationStore = fv.store
    owner = owner or fv.owner
    rows = st.query_strict("SELECT rowid AS _rowid, sample_id, at, owner FROM forward_validation_samples WHERE at = ? AND owner = ? ORDER BY rowid DESC LIMIT 1", (iso(trigger_at), owner))
    if not rows:
        bulk_samples(st.path, [healthy_sample(trigger_at, owner, last_cycle_at=iso(trigger_at - timedelta(seconds=10)))])
        rows = st.query_strict("SELECT rowid AS _rowid, sample_id, at, owner FROM forward_validation_samples WHERE at = ? AND owner = ? ORDER BY rowid DESC LIMIT 1", (iso(trigger_at), owner))
    trig = {**rows[0], "at_dt": trigger_at}
    status, res, assessment, _ = fv._assess(d, st.session(d), trig, late, escalate)
    return status, res["notes"], assessment


# ================================================================ T-S25: the HEALTHY transition matrix (design 5.6, W-04) =======================
V1_ORACLE_METHODS = ("_record_session", "_finalize", "_session_observations", "_backfill_not_run")
TS25_FAMILIES = ("base", "i", "ii", "iii", "iv", "v", "vi", "neg", "gap", "mix")
TS25_DAYS = (("2026-10-19", "2026-10-20", "2026-10-16"), ("2026-10-20", "2026-10-21", "2026-10-19"), ("2026-10-21", "2026-10-22", "2026-10-20"),
             ("2026-10-22", "2026-10-23", "2026-10-21"))                    # (d, the next market day, the previous weekday)
TS25_EARLY = ("2026-11-27", "2026-11-30", "2026-11-26")                     # the day after Thanksgiving: W.close 13:00 ET
OWNER_A, OWNER_B = "7001:aaaaaaaa", "7002:bbbbbbbb"
ROW_KINDS = ("dup", "live", "gate", "early", "failed_intent", "run_error", "refresh_fail", "broker_err")
FAILED_ROW_KINDS = ("dup", "live", "gate", "early")                         # v2's row-based FAILED evidence (5.3); a FAILED intent is class (vi)
V1_CODE_KINDS = {"DUPLICATE_BROKER_ORDER": ("dup",), "LIVE_HOST_ATTEMPT": ("live",), "SAFETY_GATE_ALLOWED_UNDER_BAD_STATE": ("gate",), "EARLY_STOP": ("early",),
                 "UNRESOLVED_EXECUTION_STATE": ("failed_intent",), "EXECUTOR_CYCLE_ERRORS": ("run_error",), "REFRESH_FAILURES": ("refresh_fail", "broker_err", "live"),
                 "BROKER_ERRORS": ("broker_err", "run_error", "live")}       # class (iv): every planted row kind that can be behind the v1 code (a superset)
CLASS_V = ("ACCOUNT_DATA_STALE_RECOVERED", "CASH_FLOW_STALE_RECOVERED", "RECONCILIATION_BLOCK_RECOVERED", "SAFETY_UNKNOWN_RECOVERED", "ACCOUNT_DATA_NEVER_FRESH",
           "CASH_FLOW_DATA_NEVER_FRESH", "SAFETY_UNKNOWN_AT_END", "BROKER_UNAVAILABLE_PERSISTENT")
LIVE_TEXT = "refused: only the PAPER host is allowed"


def v1_oracle_class():
    """T-S25's v1 oracle (W-04): 50cb2d7b's ForwardValidationService._record_session / _finalize / _session_observations / _backfill_not_run,
    copied verbatim (T-S25 pins each to 50cb2d7b by AST digest), over the current service, whose every other method they call is pinned
    hash-equal to 50cb2d7b (T-W05B). The names they read are forward_validation/service.py's own."""
    from forward_validation.service import CLOSE_MINUTE, VERSION, ForwardValidationService, M, _iso, _n, log, ny_window, to_et, weekdays_between  # noqa: F401

    class V1Oracle(ForwardValidationService):
        # ---- verbatim from 50cb2d7b forward_validation/service.py (pinned by test_TS25_the_v1_oracle_is_50cb2d7b_verbatim) ----
        def _record_session(self, now: datetime, sample: dict) -> Optional[str]:
            today = sample["trading_date"]
            et = to_et(now)
            for row in self.store.open_sessions():                                            # an earlier date still OPEN: the process was not there to close it
                if row["trading_date"] < today:
                    self._finalize(row["trading_date"], now, close_seen=False, reason="CLOSE_NOT_OBSERVED")
            if et is None or et.weekday() >= 5:
                return None
            after_close = (et.hour * 60 + et.minute) >= CLOSE_MINUTE
            row = self.store.session(today)
            if row is None:
                if after_close and not sample["in_session"]:
                    return None                                                              # first seen after the close: nothing of the session was observed
                row = self.store.open_session(today, now, self.owner, in_session=sample["in_session"])
                self.store.add_event(now, "SESSION_OPENED", None, {"in_session": sample["in_session"], "mode": sample["mode"]}, trading_date=today, owner=self.owner)
                self._touch(now, "session_recorder", "RUN", f"session {today} opened")
            if row["session_status"] != "OPEN":
                return row["session_status"]
            owners = list(row["owners"]) + ([self.owner] if self.owner not in row["owners"] else [])
            fields: Dict[str, Any] = {"bot_stop_time": sample["at"], "owners_json": __import__("json").dumps(owners),
                                      "in_session_samples": _n(row["in_session_samples"]) + (1 if sample["in_session"] else 0),
                                      "market_open_seen": bool(row["market_open_seen"]) or sample["in_session"],
                                      "paper_auto_enabled": bool(row["paper_auto_enabled"]) or (sample["in_session"] and sample["mode"] == "PAPER_AUTO")}
            self.store.update_session(today, now, **fields)
            if after_close:
                return self._finalize(today, now, close_seen=True, reason="")
            return "OPEN"

        def _finalize(self, trading_date: str, now: datetime, *, close_seen: bool, reason: str) -> str:
            row = self.store.session(trading_date)
            if row is None or row["session_status"] != "OPEN":
                return row["session_status"] if row else "NOT_RUN"
            obs = self._session_observations(trading_date, row, close_seen)
            status, notes = M.classify_session(obs)
            if reason and reason not in notes:
                notes = "|".join([c for c in notes.split("|") if c] + [reason])
            self.store.update_session(trading_date, now, ended_at=_iso(now), market_close_seen=close_seen, session_status=status, notes_code=notes,
                                      account_refresh_healthy=obs["account_healthy"], cash_flow_healthy=obs["cash_flow_healthy"], reconciliation_healthy=obs["reconciliation_healthy"])
            self.store.add_event(now, "SESSION_CLOSED", status, {"notes": notes, "in_session_samples": obs["in_session_samples"]}, trading_date=trading_date, owner=self.owner)
            self._touch(now, "session_recorder", "RUN", f"session {trading_date} closed as {status}")
            try:
                self.store.put_daily(trading_date, now, VERSION, self.daily_summary(trading_date, now))
                self._touch(now, "summary_builder", "RUN", f"daily summary {trading_date} written")
            except Exception as exc:  # noqa: BLE001 - the session record stands even when the summary failed; it is rebuilt on demand
                log.warning("forward validation: daily summary %s failed (%s)", trading_date, type(exc).__name__)
                self._touch(now, "summary_builder", "UNKNOWN", f"daily summary {trading_date} failed", type(exc).__name__)
            return status

        def _session_observations(self, trading_date: str, row: dict, close_seen: bool) -> dict:
            samples = self.store.samples(trading_date, in_session_only=True)
            start, end = ny_window(trading_date)
            x = self.execution
            fresh_seen = any(s.get("account_status") == "FRESH" for s in samples)
            cf_fresh_seen = any(s.get("cash_flow_status") == "FRESH" for s in samples)
            acct_stale = sum(1 for s in samples if s.get("account_status") in ("STALE", "UNAVAILABLE", "DISABLED"))
            cf_stale = sum(1 for s in samples if s.get("cash_flow_status") in ("STALE", "UNAVAILABLE", "AMBIGUOUS"))
            recon_bad = sum(1 for s in samples if s.get("reconciliation_healthy") is False)
            unknown = sum(1 for s in samples if s.get("safety_state") == "UNKNOWN")
            broker_unavail = sum(1 for s in samples if any("broker unavailable" in a for a in (s.get("attention") or [])))
            rel = self.order_reliability(trading_date)
            gate = self._gate_violations(start, end)
            live = self._live_host_attempts(start, end)
            runs = self.store.query("SELECT result FROM execution_runs WHERE started_at >= ? AND started_at < ?", (start, end))
            refresh_fail = _n(self.freshness(trading_date)["account"]["refresh_failures"]) + _n(self.freshness(trading_date)["cash_flow"]["refresh_failures"])
            broker_errors = _n(self.broker_health(trading_date)["total_errors"])
            early = bool(self.store.events(kind="EARLY_STOP", trading_date=trading_date))
            open_now = self._safe(lambda: x.store.open_intents(), []) or []
            pos_now = self._safe(lambda: x.store.positions(open_only=True), []) or []
            unresolved_recon = any(i.state == "RECONCILIATION_REQUIRED" for i in open_now) or any(p.state == "ATTENTION" for p in pos_now)
            failed_intents = self.store.query("SELECT count(*) AS n FROM execution_intents WHERE state = 'FAILED' AND updated_at >= ? AND updated_at < ?", (start, end))
            last = samples[-1] if samples else None
            n_in = len(samples)
            return {"in_session_samples": n_in, "market_open_seen": bool(row["market_open_seen"]) or n_in > 0, "market_close_seen": close_seen,
                    "duplicate_orders": _n(rel["duplicate_broker_orders"]), "live_host_attempts": live, "gate_violations": len(gate), "early_stop": early,
                    "unresolved_reconciliation_at_end": unresolved_recon, "unresolved_execution_at_end": _n(failed_intents[0]["n"]) if failed_intents else 0,
                    "safety_unknown_at_end": bool(last and last.get("safety_state") == "UNKNOWN"), "broker_unavailable_persistent": n_in > 0 and broker_unavail * 2 > n_in,
                    "account_stale_samples": acct_stale, "cash_flow_stale_samples": cf_stale, "reconciliation_unhealthy_samples": recon_bad, "safety_unknown_samples": unknown,
                    "cycle_errors": sum(1 for r in runs if r.get("result") == "ERROR"), "refresh_failures": refresh_fail, "broker_errors": broker_errors,
                    "restarts": len(self.store.restarts(trading_date=trading_date)), "account_fresh_seen": fresh_seen, "cash_flow_fresh_seen": cf_fresh_seen,
                    "account_healthy": n_in > 0 and fresh_seen and acct_stale == 0, "cash_flow_healthy": n_in > 0 and cf_fresh_seen and cf_stale == 0,
                    "reconciliation_healthy": n_in > 0 and recon_bad == 0 and not unresolved_recon}

        def _backfill_not_run(self, now: datetime, today: str) -> List[str]:
            last = self.store.last_session_date()
            if last is None or last >= today:
                return []
            done = []
            for d in weekdays_between(last, today):
                if self.store.session(d) is None:
                    self.store.insert_not_run(d, now, "NO_OBSERVATION|HOLIDAYS_NOT_MODELLED")
                    self.store.add_event(now, "SESSION_NOT_RUN", "NO_OBSERVATION", {"note": "no observer tick on this weekday (an exchange holiday reads the same: no calendar exists)"},
                                         trading_date=d, owner=self.owner)
                    done.append(d)
            return done
    return V1Oracle


def _at(d: str, hh: int, mm: int = 0, ss: int = 0) -> datetime:
    y, m, dd = (int(x) for x in d.split("-"))
    return et(y, m, dd, hh, mm, ss)


def _date_of(at: datetime) -> str:
    from setups.market_hours import session_date
    return (session_date(at) or at.date()).isoformat()


def ts25_trace(seed: int, family: Optional[str] = None) -> Dict[str, Any]:
    """One seeded trace (design 5.6): bulk observer samples (time-ordered), real observe() ticks with their executor stub state, evidence rows
    planted at their event time, and the restart rows _detect_restart writes at a new process's first tick. The families target each class
    (i)-(vi), the negatives (FAILED evidence in E(d), a FAILED end state), long gaps and random mixtures; every trace ends with three ticks on
    the next market day (v1's late finalize tick, v2's confirmed late path)."""
    import random
    from forward_capture import health as FH
    r = random.Random(seed)
    fam = family or TS25_FAMILIES[seed % len(TS25_FAMILIES)]
    early = fam == "v" or (fam in ("ii", "neg", "mix") and r.random() < 0.3)
    d, nxt, prev = TS25_EARLY if early else r.choice(TS25_DAYS)
    o, c, _ = FH.window(d)
    cut = c + timedelta(seconds=360)
    eod = _at(d, 23, 50)
    items: List[tuple] = []
    planted: List[Dict[str, Any]] = []
    tr: Dict[str, Any] = {"seed": seed, "family": fam, "d": d, "next": nxt, "prev": prev, "open": o, "close": c, "cut": cut, "items": items, "planted": planted,
                          "variant": ""}

    def samples(start, end, step=60, who=OWNER_A, inclusive=True, **over):
        for t in minutes(start, end, step, inclusive=inclusive):
            items.append(("s", t, who, dict(over)))

    def ticks(times, who=OWNER_A):
        for t in times:
            items.append(("t", t, who, {}))

    def ontime(who=OWNER_A):
        ticks([c + timedelta(minutes=k) for k in (5, 10, 15)], who)

    def evening(who=OWNER_A):
        ticks([_at(d, 17, 30) + timedelta(minutes=k) for k in (0, 1, 2)], who)

    def row(at, fn):
        items.append(("r", at, None, fn))

    def plant(kind, at, **extra):
        rec = {"kind": kind, "at": at, **extra}
        planted.append(rec)
        if kind == "dup":
            coid = f"stockagent-entry-ts25{seed:05d}{len(planted):02d}"
            for m in rec["members"]:
                row(m, lambda db, m=m: add_order(db, coid, m))
        elif kind == "live":
            row(at, lambda db: add_refresh(db, at, result="FAILED", error_text=LIVE_TEXT))
        elif kind == "gate":
            row(at - timedelta(seconds=30), lambda db: add_safety_eval(db, at - timedelta(seconds=30), state="NO_NEW_ENTRIES", allowed=False))
            row(at, lambda db: add_intent(db, at, submitted=at))
            row(at + timedelta(seconds=20), lambda db: add_safety_eval(db, at + timedelta(seconds=20)))   # the gate is healthy again
        elif kind == "early":
            row(at, lambda db: add_event(db, at, "EARLY_STOP", "TS25", {"stopped": False}, trading_date=d))
        elif kind == "failed_intent":
            row(at, lambda db: add_intent(db, at, state="FAILED", updated=at))
        elif kind == "run_error":
            row(at, lambda db: add_run(db, at, result="ERROR", error="BROKER_TIMEOUT: ts25"))
        elif kind == "refresh_fail":
            row(at, lambda db: add_refresh(db, at, result="FAILED"))
        elif kind == "broker_err":
            row(at, lambda db: add_refresh(db, at, result="FAILED", error_code="HTTP_503"))
        elif kind == "rr":
            row(at, lambda db: add_intent(db, at, state="RECONCILIATION_REQUIRED"))
        elif kind == "att":
            row(at, lambda db: add_position(db, at, state="ATTENTION"))

    def restart(at, prev_owner, prev_at, new_owner):
        items.append(("x", at, None, {"at": iso(at), "trading_date": _date_of(at), "previous_owner": prev_owner, "previous_sample_at": iso(prev_at), "owner": new_owner}))

    def secs(lo, hi):
        return timedelta(seconds=r.randint(int(lo), int(hi)))

    def row_kind_at(kind, lo, hi):
        at = lo + secs(0, (hi - lo).total_seconds())
        if kind == "dup":
            first = _at(d, 11, 0) + secs(0, 3600)
            plant("dup", at, members=[first, at])
        else:
            plant(kind, at)

    last_owner = OWNER_A
    if fam == "base":
        samples(o, c, r.choice((60, 60, 90)))
        (ontime if r.random() < 0.7 else evening)()
    elif fam == "i":                                                                   # a restart before the open (P1E), with or without a takeover
        a_end = _at(d, 9, r.randint(8, 22))
        samples(_at(d, 9, 0), a_end, 60, OWNER_A)
        b0 = a_end + timedelta(seconds=30)
        if r.random() < 0.5:                                                           # a takeover: both processes write until A stops
            tr["variant"] = "takeover"
            b0 = a_end - timedelta(minutes=r.randint(2, 6), seconds=30)
            samples(b0, a_end + timedelta(seconds=30), 60, OWNER_B)
            restart(b0, OWNER_A, b0 - timedelta(seconds=30), OWNER_B)
            samples(a_end + timedelta(seconds=90), c, 60, OWNER_B)
        else:
            restart(b0, OWNER_A, a_end, OWNER_B)
            samples(b0, c, 60, OWNER_B)
        last_owner = OWNER_B
        r.choice((ontime, evening, lambda who=OWNER_B: None))(OWNER_B)
    elif fam == "ii":                                                                  # the close observed within G, v1's tick only on a later date
        if early:
            samples(o, c, 60)
            ontime()
            tr["variant"] = "early close"
        else:
            samples(o, c - timedelta(minutes=r.randint(1, 6)), 60)
    elif fam == "iii":                                                                 # v1's late live read of a state that arose after C
        samples(o, c - timedelta(minutes=r.randint(1, 5)), 60)
        late = r.random() < 0.6
        if not late:
            evening()
        hi = _at(d, 17, 25) if not late else eod
        plant(r.choice(("rr", "att")), cut + secs(60, (hi - cut).total_seconds()))
    elif fam == "iv":                                                                  # row evidence after the cutoff, on the same NY day
        samples(o, c - timedelta(minutes=r.randint(1, 5)), 60)
        late = r.random() < 0.6
        if not late:
            evening()
        hi = _at(d, 17, 25) if not late else eod
        for kind in r.sample(ROW_KINDS, r.randint(1, 2)):
            row_kind_at(kind, cut + timedelta(seconds=60), hi)
    elif fam == "v":                                                                   # the early-close afternoon v1 counts as its session
        samples(o, c, 60)
        ontime()
        cond = r.choice(("acct", "cf", "recon", "unknown", "unknown_end", "broker"))
        tr["variant"] = cond
        step = 30 if cond == "broker" else r.choice((60, 120))
        aft_end = _at(d, 15, r.randint(50, 59))
        over = {"acct": {"account_status": "STALE"}, "cf": {"cash_flow_status": "STALE"}, "recon": {"reconciliation_healthy": False},
                "unknown": {"safety_state": "UNKNOWN"}, "unknown_end": {}, "broker": {"attention": ["SNDK: broker unavailable"], "attention_count": 1}}[cond]
        times = minutes(c + timedelta(minutes=16), aft_end, step)
        for i, t in enumerate(times):
            bad = cond == "broker" or (cond == "unknown_end" and i == len(times) - 1) or (cond not in ("broker", "unknown_end") and i % 3 == 1)
            items.append(("s", t, OWNER_A, dict(over if bad and cond != "unknown_end" else ({"safety_state": "UNKNOWN"} if bad else {}))))
        if r.random() < 0.3:
            samples(_at(d, 16, 0), _at(d, 16, 0), 60)                                  # a tick at 16:00: v1's on-time finalize
    elif fam == "vi":                                                                  # a FAILED intent after the last end-state sample, before C (W-03)
        last = c - timedelta(minutes=r.randint(1, 5))
        samples(o, last, 60)
        plant("failed_intent", last + secs(10, (cut - last).total_seconds() - 5))
    elif fam == "neg":                                                                 # never HEALTHY: FAILED evidence in E(d) or a FAILED end state
        kind = r.choice(("early_in", "dup_in", "live_in", "gate_in", "rr_grace", "failed_grace", "unknown_end", "att_end", "broker_s"))
        tr["variant"] = kind
        times = minutes(o, c, 60)
        bad_last = {"unknown_end": {"safety_state": "UNKNOWN"}, "att_end": {"attention_positions": 1}}.get(kind)
        for i, t in enumerate(times):
            over = dict(bad_last) if bad_last and i == len(times) - 1 else {}
            if kind == "broker_s" and i <= len(times) // 2:
                over = {"attention": ["SNDK: broker unavailable"], "attention_count": 1}
            items.append(("s", t, OWNER_A, over))
        d0 = _at(d, 0, 0)
        if kind in ("early_in", "live_in", "gate_in"):
            plant(kind[:-3], d0 + timedelta(hours=2) + secs(0, (cut - d0).total_seconds() - 7300))
        elif kind == "dup_in":
            a1 = _at(d, 10, 0) + secs(0, 3 * 3600)
            plant("dup", a1 + timedelta(seconds=40), members=[a1, a1 + timedelta(seconds=40)])
        elif kind == "rr_grace":
            plant("rr", c + timedelta(seconds=r.randint(20, 200)))
        elif kind == "failed_grace":
            plant("failed_intent", c + timedelta(seconds=r.randint(20, 200)))
        if kind in ("rr_grace", "failed_grace"):
            ontime()
        else:
            r.choice((ontime, evening, lambda who=OWNER_A: None))()
    elif fam == "gap":                                                                 # the gap rule: a close or interior gap over G
        if r.random() < 0.6:
            tr["variant"] = "close gap"
            samples(o, c - timedelta(minutes=r.randint(7, 40)), 60)
        else:
            tr["variant"] = "interior gap"
            g0 = _at(d, 11, 0) + secs(0, 7200)
            samples(o, g0, 60)
            samples(g0 + timedelta(minutes=r.randint(7, 30)), c, 60)
            r.choice((ontime, lambda who=OWNER_A: None))()
    else:                                                                              # "mix": sparse, late starts, NULL counts, suspects, takeovers, rows
        step = r.choice((60, 60, 120, 300, 420))
        start = r.choice((_at(d, 9, 0), o, o + timedelta(minutes=r.randint(1, 20)), c + timedelta(minutes=2)))
        end = r.choice((c, c, c - timedelta(minutes=r.randint(1, 30))))
        null_p = 0.2 if r.random() < 0.3 else 0.0
        for t in minutes(start, max(start, end), step):
            items.append(("s", t, OWNER_A, {"rr_intents": None} if r.random() < null_p else {}))
        if r.random() < 0.15 and start < end - timedelta(hours=2):                    # a clock jump forward: a suspect sample
            tr["variant"] += " suspect"
            items.append(("s", start + timedelta(minutes=30, seconds=7), OWNER_A, {"_skew_s": r.randint(600, 5400)}))
        if r.random() < 0.15 and start < _at(d, 12, 0) < end:                          # a takeover inside the session
            tr["variant"] += " takeover"
            b0 = _at(d, 12, 0) + timedelta(seconds=30)
            restart(b0, OWNER_A, _at(d, 12, 0), OWNER_B)
            samples(b0, b0 + timedelta(minutes=10), 60, OWNER_B)
        for _ in range(r.choice((0, 0, 1, 2))):
            row_kind_at(r.choice(ROW_KINDS), _at(d, 0, 30), eod)
        r.choice((ontime, ontime, evening, lambda who=OWNER_A: None))()
    ticks([_at(nxt, 9, 0) + timedelta(minutes=k) for k in (0, 1, 2)], last_owner)
    return tr


def ts25_v1_tick(tr) -> Optional[datetime]:
    """v1's finalize tick (design 5.6): the first tick at or after 16:00 ET on d, else the first tick of a later NY date."""
    times = sorted(it[1] for it in tr["items"] if it[0] in ("s", "t") and "_skew_s" not in it[3])
    on_d = [t for t in times if _date_of(t) == tr["d"] and t.astimezone(NY).hour >= 16]
    later = [t for t in times if _date_of(t) > tr["d"]]
    return on_d[0] if on_d else (later[0] if later else None)


def _v1_classify(lab, tr, t_v1: datetime) -> Dict[str, Any]:
    """The v1 oracle on its OWN copy of the trace's database at v1's finalize tick (W-04): v1's session rows only (d's row as v1 opened it,
    the previous weekday's closed row so a never-opened d is back-filled), then v1's observe order at that tick: _backfill_not_run, _record_session."""
    from execution.store import ExecutionStore
    from forward_validation.store import ForwardValidationStore
    copy = lab.db.with_name(lab.db.stem + f"_v1_{tr['seed']}.db")
    src, dst = sqlite3.connect(str(lab.db)), sqlite3.connect(str(copy))
    try:
        src.backup(dst)
        guards = dst.execute("SELECT name, sql FROM sqlite_master WHERE type = 'trigger' AND name LIKE '%no_delete' AND tbl_name IN "
                             "('forward_validation_restarts', 'forward_validation_events', 'forward_validation_sessions')").fetchall()
        for name, _ in guards:                                                         # the oracle's own copy: lifted for these deletes only
            dst.execute(f'DROP TRIGGER "{name}"')
        dst.execute("DELETE FROM forward_validation_restarts WHERE at >= ?", (iso(t_v1),))         # that tick's own later writes (restart, early stop)
        dst.execute("DELETE FROM forward_validation_events WHERE at >= ?", (iso(t_v1),))
        dst.execute("DELETE FROM forward_validation_sessions")
        for _, sql in guards:
            dst.execute(sql)                                                           # v1 then runs under every trigger of the schema
        dst.commit()
    finally:
        src.close()
        dst.close()
    store = ForwardValidationStore(copy)
    d = tr["d"]
    store.open_session(tr["prev"], _at(tr["prev"], 9, 30), "v1-seed", in_session=True)
    store.update_session(tr["prev"], _at(tr["prev"], 16, 30), session_status="HEALTHY")
    mine = [s for s in store.samples(d, limit=100000) if s["at"] <= iso(t_v1)]
    mine.sort(key=lambda s: s["at"])
    if mine and (datetime.fromisoformat(mine[0]["at"]).astimezone(NY).hour < 16 or mine[0]["in_session"]):
        first = mine[0]
        store.open_session(d, datetime.fromisoformat(first["at"]), first["owner"], in_session=bool(first["in_session"]))
        before = [s for s in mine if s["at"] < iso(t_v1)]
        store.update_session(d, t_v1, market_open_seen=any(s["in_session"] for s in before), in_session_samples=sum(1 for s in before if s["in_session"]))
    stream = SimpleNamespace(execution=_Exec(ExecutionStore(copy)), safety=lab.stream.safety, account_refresh=lab.stream.account_refresh, cash_flow=lab.stream.cash_flow)
    oracle = v1_oracle_class()(lambda: stream, store, owner=lab.owner, early_stop_enabled=True)
    rows = store.query_strict("SELECT * FROM forward_validation_samples WHERE at = ? ORDER BY rowid DESC LIMIT 1", (iso(t_v1),))
    sample = ForwardValidationStore._sample(rows[0])
    oracle._backfill_not_run(t_v1, sample["trading_date"])
    oracle._record_session(t_v1, sample)
    row = store.session(d)
    if row is None:
        return {"status": "NO_ROW", "codes": [], "hidden": [], "t": t_v1}
    codes = [x for x in (row.get("notes_code") or "").split("|") if x]
    hidden: List[str] = []
    if row["session_status"] == "FAILED":                                              # the DEGRADED codes a v1 FAILED does not show (its notes list FAILED codes only)
        from forward_validation import metrics as M
        on_time = _date_of(t_v1) == d
        obs = oracle._session_observations(d, row, on_time)
        clear = {"duplicate_orders": 0, "live_host_attempts": 0, "gate_violations": 0, "early_stop": False, "unresolved_reconciliation_at_end": False,
                 "unresolved_execution_at_end": 0, "safety_unknown_at_end": False, "broker_unavailable_persistent": False}
        hidden = [x for x in M.classify_session({**obs, **clear})[1].split("|") if x and x not in codes] + ([] if on_time else ["CLOSE_NOT_OBSERVED"])
    return {"status": row["session_status"], "codes": codes, "hidden": sorted(set(hidden) - set(codes)), "t": t_v1}


def ts25_run(tr, root: Path, monkeypatch, template: Path) -> Dict[str, Any]:
    """Run one trace: the items in time order through bulk inserts, planted rows and real observe() ticks (v2), the v1 oracle at v1's
    finalize tick; then the trace facts the class predicates read (from the stored samples and the planted rows, not from v2)."""
    first_tick = next((it for it in sorted(tr["items"], key=lambda x: x[1]) if it[0] == "t"), None)
    lab = Lab(root / f"ts25_{tr['seed']}", monkeypatch, owner=first_tick[2] if first_tick else OWNER_A, template=template)
    lab.fv._restart_checked = True                                                     # restarts are planted where the new process's first tick wrote them
    t_v1 = ts25_v1_tick(tr)
    batch: List[Dict[str, Any]] = []
    v1 = None

    def flush():
        bulk_samples(lab.db, batch)
        batch.clear()
    prio = {"r": 0, "x": 0, "s": 1, "t": 2}
    for kind, at, who, payload in sorted(tr["items"], key=lambda x: (x[1], prio[x[0]])):
        if v1 is not None and _date_of(at) > tr["d"]:                                   # D-4: both results already fixed - v1 evaluated and d's v2 row closed
            flush()                                                                     # (frozen by B-29); a later date's ticks cannot change either
            row = lab.session(tr["d"])
            if row is not None and row["session_status"] != "OPEN":
                break
        if kind == "s":
            over = dict(payload)
            skew = over.pop("_skew_s", None)
            batch.append(healthy_sample(at + timedelta(seconds=skew) if skew else at, who, **over))
        else:
            flush()
            if kind == "r":
                payload(lab.db)
            elif kind == "x":
                lab.store.add_restart(payload)
            else:
                if lab.owner != who:
                    lab.new_process(who)
                    lab.fv._restart_checked = True
                lab.tick(at)
        if v1 is None and t_v1 is not None and at == t_v1 and kind in ("s", "t") and "_skew_s" not in payload:
            flush()
            v1 = _v1_classify(lab, tr, t_v1)
    flush()
    row = lab.session(tr["d"])
    v2 = {"status": row["session_status"] if row else "NO_ROW", "notes": list(row["notes"]) if row else [], "assessment": (row or {}).get("assessment") or {}}
    return {"trace": tr, "v1": v1 or {"status": "NO_TICK", "codes": [], "hidden": [], "t": None}, "v2": v2, "facts": ts25_facts(lab, tr, t_v1)}


def ts25_facts(lab, tr, t_v1) -> Dict[str, Any]:
    """The trace as the class predicates read it, computed independently of v2: suspects (a later-inserted sample more than h earlier),
    S(d), the grace samples, the end-state set, the restart times, the FAILED evidence planted in E(d) and a FAILED end state."""
    rows = lab.store.query_strict("SELECT rowid AS rid, at, owner, attention_json, account_status, cash_flow_status, reconciliation_healthy, safety_state, rr_intents, "
                                  "attention_positions, failed_intents FROM forward_validation_samples ORDER BY rowid")
    for s in rows:
        s["at"] = datetime.fromisoformat(s["at"])
        s["attention"] = json.loads(s.pop("attention_json") or "[]")
    suspect, later_min = set(), None
    for s in reversed(rows):
        if later_min is not None and later_min < s["at"] - timedelta(seconds=60):
            suspect.add(s["rid"])
        later_min = s["at"] if later_min is None else min(later_min, s["at"])
    o, c, cut, d = tr["open"], tr["close"], tr["cut"], tr["d"]
    clean = [s for s in rows if s["rid"] not in suspect]
    S = sorted((s for s in clean if o <= s["at"] <= c), key=lambda s: (s["at"], s["rid"]))
    grace = sorted((s for s in clean if c < s["at"] <= cut), key=lambda s: (s["at"], s["rid"]))
    end = S[-1:] + grace
    restarts = [b["at"] for a, b in zip(rows, rows[1:]) if a["owner"] != b["owner"] and _date_of(b["at"]) == d]
    d0 = _at(d, 0, 0)
    failed_in_e = []
    for p in tr["planted"]:
        if p["kind"] == "dup":
            if sum(1 for m in p["members"] if m <= cut) >= 2 and any(d0 <= m <= cut for m in p["members"]):
                failed_in_e.append("dup")
        elif p["kind"] in FAILED_ROW_KINDS and d0 <= p["at"] <= cut:
            failed_in_e.append(p["kind"])
    end_failed = [k for k, pred in (("rr", lambda s: (s["rr_intents"] or 0) > 0 or (s["attention_positions"] or 0) > 0), ("failed", lambda s: (s["failed_intents"] or 0) > 0),
                                    ("unknown", lambda s: s["safety_state"] == "UNKNOWN")) if any(pred(s) for s in end)]
    if S and 2 * sum(1 for s in S if any("broker unavailable" in str(a) for a in s["attention"])) > len(S):
        end_failed.append("broker_unavailable")
    return {"S": S, "grace": grace, "end": end, "restarts": restarts, "failed_in_E": failed_in_e, "end_failed": end_failed, "t_v1": t_v1, "d0": d0,
            "d1": _at((date.fromisoformat(d) + timedelta(days=1)).isoformat(), 0, 0)}


def _class_v_fires(code: str, X: List[Dict[str, Any]]) -> bool:
    """v1's own predicate of a class (v) code, evaluated over S(d) and the end-state set instead of v1's in_session samples."""
    if not X:
        return False
    if code == "ACCOUNT_DATA_STALE_RECOVERED":
        return any(s["account_status"] in ("STALE", "UNAVAILABLE", "DISABLED") for s in X)
    if code == "CASH_FLOW_STALE_RECOVERED":
        return any(s["cash_flow_status"] in ("STALE", "UNAVAILABLE", "AMBIGUOUS") for s in X)
    if code == "RECONCILIATION_BLOCK_RECOVERED":
        return any(s["reconciliation_healthy"] == 0 for s in X)
    if code == "SAFETY_UNKNOWN_RECOVERED":
        return any(s["safety_state"] == "UNKNOWN" for s in X)
    if code == "ACCOUNT_DATA_NEVER_FRESH":
        return not any(s["account_status"] == "FRESH" for s in X)
    if code == "CASH_FLOW_DATA_NEVER_FRESH":
        return not any(s["cash_flow_status"] == "FRESH" for s in X)
    if code == "SAFETY_UNKNOWN_AT_END":
        return X[-1]["safety_state"] == "UNKNOWN"
    if code == "BROKER_UNAVAILABLE_PERSISTENT":
        return 2 * sum(1 for s in X if any("broker unavailable" in str(a) for a in s["attention"])) > len(X)
    raise AssertionError(code)


def ts25_explain(code: str, res: Dict[str, Any]) -> Optional[str]:
    """The class (i)-(vi) of design 5.6 whose predicate explains the v1 code on this trace, else None."""
    tr, f = res["trace"], res["facts"]
    o, c, cut, t_v1 = tr["open"], tr["close"], tr["cut"], f["t_v1"]
    if code == "RESTART_DURING_SESSION":
        return "i" if not any(o < t <= c for t in f["restarts"]) else None
    if code == "CLOSE_NOT_OBSERVED":
        return "ii" if f["S"] and (c - f["S"][-1]["at"]).total_seconds() <= 360 else None
    if code == "UNRESOLVED_RECONCILIATION":
        return "iii" if t_v1 > cut and not any((s["rr_intents"] or 0) > 0 or (s["attention_positions"] or 0) > 0 for s in f["end"]) else None
    if code in V1_CODE_KINDS:
        seen = [p for p in tr["planted"] if p["kind"] in V1_CODE_KINDS[code] and f["d0"] <= p["at"] < min(f["d1"], t_v1)]
        if seen and all((sum(1 for m in p["members"] if m <= cut) < 2) if p["kind"] == "dup" else p["at"] > cut for p in seen):
            return "iv"
        if code == "UNRESOLVED_EXECUTION_STATE" and seen and t_v1 > cut and f["end"] and all(p["at"] > f["end"][-1]["at"] for p in seen) \
                and not any((s["failed_intents"] or 0) > 0 for s in f["end"]):
            return "vi"
        return None
    if code in CLASS_V:
        X = {s["rid"]: s for s in f["S"] + f["end"]}
        return "v" if not _class_v_fires(code, sorted(X.values(), key=lambda s: (s["at"], s["rid"]))) else None
    return None


def ts25_check(res: Dict[str, Any]) -> Tuple[List[str], set]:
    """The property (test plan T-S25): (violations, the classes that explained a v1 code on this trace)."""
    v1, v2, f = res["v1"], res["v2"], res["facts"]
    problems: List[str] = []
    used: set = set()
    if v2["status"] != "HEALTHY":
        return problems, used
    if f["failed_in_E"]:
        problems.append(f"v2 HEALTHY with FAILED evidence in E(d): {f['failed_in_E']}")
    if f["end_failed"]:
        problems.append(f"v2 HEALTHY with a FAILED end state: {f['end_failed']}")
    if v1["status"] == "HEALTHY":
        return problems, used
    if v1["status"] in ("NOT_RUN", "NO_ROW", "NO_TICK"):
        return problems + [f"v1 {v1['status']} -> v2 HEALTHY"], used
    if v1["status"] not in ("DEGRADED", "FAILED"):
        return problems + [f"v1 status {v1['status']!r} unexpected"], used
    for code in v1["codes"] + v1.get("hidden", []):
        cls = ts25_explain(code, res)
        if cls is None:
            problems.append(f"v1 {code} unexplained")
        else:
            used.add(cls)
    return problems, used
