"""
Wave 2 PR C test support (WAVE2_PRC_TEST_PLAN_REV4.md Revision 4.3): synthetic data only, tmp_path databases, no network.

  migrate()          every table a forward run reads (PR B's set plus the 6.10A capture, setup, proposal and lease tables)
  config()           a complete 6.10A config snapshot (every C-11 drift field non-null); put_snapshot() writes one row
  put_session()      a final (or OPEN) Stage 6.1 session row of a classifier version; put_health() a 6.10A health record
  day_samples()      a time-ordered minute cadence of run-stamped observer samples over a market window
  RunLab             PR B's Lab plus a forward run: ready() makes run_admin start possible at a time; start() runs it
  trade()            a bot position with its entry / exit intents, order events (raw filled_at / updated_at), pnl snapshots and marks
"""
from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

import wave2_prb_support as B
from wave2_prb_support import et, iso  # noqa: F401 - re-exported

COHORT = "FP-run000000001"
BLOCKERS = ["PF-18", "EX-13", "PF-08", "W2-N23", "W2-N03"]


def migrate(db: Path) -> None:
    from database.automation_migrations import run_automation_migrations
    from database.forward_capture_migrations import run_forward_capture_migrations
    from database.proposal_migrations import run_proposal_migrations
    from database.setup_state_migrations import run_setup_state_migrations
    B.migrate(db)
    conn = sqlite3.connect(str(db))
    try:
        for fn in (run_setup_state_migrations, run_proposal_migrations, run_automation_migrations, run_forward_capture_migrations):
            fn(conn)
        conn.commit()
    finally:
        conn.close()


def sql(db: Path, statement: str, args=()) -> None:
    conn = sqlite3.connect(str(db))
    try:
        conn.execute(statement, args)
        conn.commit()
    finally:
        conn.close()


def rows(db: Path, statement: str, args=()) -> List[dict]:
    conn = sqlite3.connect(str(db))
    conn.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in conn.execute(statement, args).fetchall()]
    finally:
        conn.close()


def config(**over) -> Dict[str, Any]:
    """A complete snapshot config: the required flags true, the cadences 60 / 300 s, every drift field non-null. `over` replaces whole groups
    or dotted keys (e.g. {"flags.FORWARD_VALIDATION_ENABLED": False})."""
    import execution as X
    from forward_capture.cohort import CADENCES, EXECUTOR_LIMITS, FLAGS
    cfg = {"flags": {f: True for f in FLAGS}, "cadences": {c: 60 for c in CADENCES}, "executor_limits": {k: 1 for k in EXECUTOR_LIMITS},
           "universe": {"symbols": ["SNDK"], "count": 1, "sha256": "u" * 64}, "autorun": {"policy": "MANUAL_ARM", "effective_policy": "MANUAL_ARM",
                                                                                       "scheduled_arm_enabled": False, "disarm_at_session_end": True, "arm_delay_minutes": 5},
           "market_data_mode": {"configured": "iex", "resolved": "iex"}, "calendar": "nyse_v1", "versions": {"execution": X.VERSION, "setups": "s1"},
           "entry_controls": {"version": "entry_controls_v2", "contract_hash": "c" * 64, "gate_enabled": False}, "broker": "alpaca_paper",
           "app": {"app_root": "x", "db_name": "y"}, "session_policy": "regular session only"}
    cfg["cadences"]["FORWARD_VALIDATION_IDLE_SECONDS"] = 300
    for k, v in over.items():
        if "." in k:
            g, n = k.split(".", 1)
            cfg[g] = {**(cfg.get(g) or {}), n: v}
        else:
            cfg[k] = v
    return cfg


def put_snapshot(db: Path, at: datetime, *, owner: str = "7001:aaaaaaaa", cohort_id: str = COHORT, cfg: Optional[dict] = None, config_hash: str = "cfg1") -> None:
    sql(db, "INSERT INTO forward_capture_config_snapshots (snapshot_id, at, trigger, trading_date, config_hash, cohort_id, config_json, owner) VALUES (?,?,?,?,?,?,?,?)",
        (uuid.uuid4().hex, iso(at), "DAY", None, config_hash, cohort_id, json.dumps(cfg if cfg is not None else config(), sort_keys=True), owner))


def put_health(db: Path, d: str, status: str = "HEALTHY", *, final: bool = True, cohort_ids=(COHORT,), reasons=()) -> None:
    sql(db, "INSERT INTO forward_capture_session_health (trading_date, status, final, reasons_json, metrics_json, summary_text, cohort_ids_json, source_session_status, "
            "computed_at, version) VALUES (?,?,?,?,?,?,?,?,?,?)", (d, status, 1 if final else 0, json.dumps(list(reasons)), "{}", None, json.dumps(list(cohort_ids)), None,
                                                                  "2026-01-01T00:00:00+00:00", "t"))


def put_session(db: Path, d: str, status: str = "HEALTHY", *, notes: str = "", classifier_version: Optional[str] = "session_classifier_v2_1", at: Optional[datetime] = None) -> None:
    """A Stage 6.1 row: inserted OPEN, then closed in one update (the 3b triggers freeze a closed row); NOT_RUN is inserted final."""
    stamp = iso(at) if at else f"{d}T21:30:00+00:00"
    if status == "NOT_RUN":
        sql(db, "INSERT INTO forward_validation_sessions (trading_date, session_status, notes_code, in_session_samples, owners_json, created_at, updated_at, classifier_version) "
                "VALUES (?, 'NOT_RUN', ?, 0, '[]', ?, ?, ?)", (d, notes, stamp, stamp, classifier_version))
        return
    sql(db, "INSERT INTO forward_validation_sessions (trading_date, started_at, session_status, notes_code, in_session_samples, owners_json, created_at, updated_at) "
            "VALUES (?, ?, 'OPEN', '', 0, '[]', ?, ?)", (d, stamp, stamp, stamp))
    if status != "OPEN":
        sql(db, "UPDATE forward_validation_sessions SET session_status = ?, notes_code = ?, classifier_version = ?, updated_at = ? WHERE trading_date = ?",
            (status, notes, classifier_version, stamp, d))


def stamped(at: datetime, owner: str, run_id: Optional[str], fh: Optional[str], **over) -> Dict[str, Any]:
    return B.healthy_sample(at, owner, **{"frozen_hash": fh, "run_id": run_id, "equity": "100000.00", **over})


def day_samples(db: Path, d: str, owner: str, run_id: Optional[str], fh: Optional[str], *, step_s: int = 60, start: Optional[datetime] = None,
                end: Optional[datetime] = None, **over) -> int:
    from forward_capture import health as FH
    w = FH.window(d)
    assert w is not None, d
    times = B.minutes(start or w[0], end or w[1], step_s)
    return B.bulk_samples(db, (stamped(t, owner, run_id, fh, **over) for t in times))


def member_day(r, d: str, run: dict, *, mode: str = "MONITOR_ONLY", status: str = "HEALTHY", notes: str = "", health: str = "HEALTHY", health_final: bool = True,
               health_reasons=(), cohorts=(COHORT,), samples: bool = True, classifier_version: str = "session_classifier_v2_1", **sample_over) -> None:
    """One market day of a run as the observer and the capture leave it: run-stamped minute samples over the window, a final 6.1 row of the
    run's classifier version and a final 6.10A record of the run's cohort."""
    if samples:
        day_samples(r.db, d, r.owner, run["run_id"], run["frozen_hash"], mode=mode, **sample_over)
    put_session(r.db, d, status, notes=notes, classifier_version=classifier_version)
    put_health(r.db, d, health, final=health_final, cohort_ids=tuple(cohorts), reasons=health_reasons)


def put_decision(db: Path, execution_id: str, at: datetime, *, cohort_id: Optional[str] = COHORT, decision: str = "ENTERED", recovered: int = 0, legacy: int = 0) -> None:
    sql(db, "INSERT INTO forward_capture_decisions (decision_id, symbol, cohort_id, decided_at, observed_at, trading_date, source, decision, execution_id, recovered, legacy, "
            "content_sha256) VALUES (?, 'SNDK', ?, ?, ?, ?, 'INTENT', ?, ?, ?, ?, ?)",
        (uuid.uuid4().hex, cohort_id, iso(at), iso(at), at.date().isoformat(), decision, execution_id, recovered, legacy, "d" * 64))


def put_recon(db: Path, at: datetime, symbol: str, detail_json: str, kind: str = "MULTIPLE_ORDERS") -> int:
    """One executor reconciliation finding written as _reconcile writes it (append-only table: inserted, never updated). Returns its rowid."""
    conn = sqlite3.connect(str(db))
    try:
        cur = conn.execute("INSERT INTO execution_reconciliations (reconciliation_id, run_id, at, trigger, kind, symbol, blocking, detail_json) VALUES (?, NULL, ?, 'CYCLE', ?, ?, 1, ?)",
                           (uuid.uuid4().hex, iso(at), kind, symbol, detail_json))
        conn.commit()
        return int(cur.lastrowid)
    finally:
        conn.close()


# L-2: the MULTIPLE_ORDERS details _reconcile writes (C-64), by ownership case; counted = W2-6's canonical predicate (C-65). L-b: a row whose
# ownership values do not establish anything (null, mistyped, a negative or a bool count) is counted, like a legacy row (fail-closed).
L2_CASES = {
    "manual_unrelated": ('{"count": 2, "client_order_ids": ["manual-1", "manual-2"], "bot_orders": 0, "bot_exposed": false}', False),
    "bot_owned": ('{"count": 2, "client_order_ids": ["stockagent-a", "stockagent-b"], "bot_orders": 2, "bot_exposed": false}', True),
    "mixed_exposed": ('{"count": 2, "client_order_ids": ["manual-1", "stockagent-a"], "bot_orders": 1, "bot_exposed": true}', True),
    "legacy_no_keys": ('{"count": 2}', True),
    "unparsable": ("not json", True),
    "null_ownership": ('{"count": 2, "client_order_ids": ["manual-1", "manual-2"], "bot_orders": null, "bot_exposed": null}', True),
    "mistyped_ownership": ('{"count": 2, "client_order_ids": ["manual-1", "manual-2"], "bot_orders": "0", "bot_exposed": 0}', True),
    "negative_bot_orders": ('{"count": 2, "client_order_ids": ["manual-1", "manual-2"], "bot_orders": -1, "bot_exposed": false}', True),
    "bool_bot_orders": ('{"count": 2, "client_order_ids": ["manual-1", "manual-2"], "bot_orders": true, "bot_exposed": false}', True),
    "exposure_null": ('{"count": 2, "client_order_ids": ["manual-1", "manual-2"], "bot_orders": 0, "bot_exposed": null}', True),   # a valid count, exposure unknown
    "exposure_mistyped": ('{"count": 2, "client_order_ids": ["manual-1", "stockagent-a"], "bot_orders": 1, "bot_exposed": 0}', True),
    "exposure_missing": ('{"count": 2, "client_order_ids": ["manual-1", "manual-2"], "bot_orders": 0}', True),
}
LB_UNESTABLISHED = ("null_ownership", "mistyped_ownership", "negative_bot_orders", "bool_bot_orders", "exposure_null", "exposure_mistyped", "exposure_missing")


def fresh_marks(a: datetime, b: datetime, px: str = "10.50", step_s: int = 60) -> List[tuple]:
    """Observer marks every `step_s` from a to b, each with a fresh last_price_at (its own time)."""
    return [(t, px, t) for t in B.minutes(a, b, step_s)]


def health_610a(r, d: str, now: datetime) -> dict:
    """The REAL Stage 6.10A record of `d` at `now` (forward_capture.health.compute over this database), written as the capture writes it."""
    from types import SimpleNamespace
    from forward_capture import VERSION
    from forward_capture import health as FH
    from forward_capture.store import ForwardCaptureStore
    cap = SimpleNamespace(store=ForwardCaptureStore(r.db), fv=r.fv)
    rec = FH.compute(cap, d, now)
    cap.store.put_health({**rec, "reasons_json": rec["reasons"], "metrics_json": rec["metrics"], "cohort_ids_json": rec["cohort_ids"], "computed_at": iso(now), "version": VERSION})
    return rec


class RunLab(B.Lab):
    """PR B's lightweight observer lab on a database with every forward-run table."""

    def __init__(self, tmp_path: Path, monkeypatch, **kw):
        Path(tmp_path).mkdir(parents=True, exist_ok=True)
        db = Path(tmp_path) / kw.pop("name", "w2c.db")
        migrate(db)
        super().__init__(tmp_path, monkeypatch, name=db.name, template=None, **kw)

    @property
    def fh(self) -> str:
        import forward_validation as FV
        return FV.frozen_hash()

    def ready(self, now: datetime, *, sample_over: Optional[dict] = None, snapshot: bool = True, cfg: Optional[dict] = None) -> None:
        """The rows a start at `now` needs: a fresh PR-B-and-C sample 30 s before, a complete config snapshot 60 s before, no position, no
        intent, no lease, no fault, an empty capture backlog."""
        if snapshot:
            put_snapshot(self.db, now - timedelta(seconds=60), owner=self.owner, cfg=cfg)
        B.bulk_samples(self.db, [stamped(now - timedelta(seconds=30), self.owner, "", self.fh, **(sample_over or {}))])

    def start(self, now: datetime, **opts) -> dict:
        from forward_validation import run_admin
        o = {"approved_by": "operator", "evidence_ref": "DW2K-1", "accept_blockers": list(BLOCKERS)}
        o.update(opts)
        return run_admin.start(self.store, now, o)

    def run_row(self) -> Optional[dict]:
        return self.store.active_run()


def trade(db: Path, *, symbol: str = "SNDK", entry_at: datetime, exit_at: Optional[datetime], qty: int = 10, entry_px: str = "10.00", exit_px: Optional[str] = "11.00",
          realized: Optional[str] = None, created_at: Optional[datetime] = None, filled_at: Optional[datetime] = None, exit_filled_at: Optional[datetime] = None,
          marks: Iterable[tuple] = (), snapshot_cohort: Optional[str] = COHORT, coid_prefix: str = "sa-", exit_submitted_at: Optional[datetime] = None,
          entry_raw: Optional[dict] = None, exit_raw: Optional[dict] = None) -> Dict[str, str]:
    """A bot position: the entry intent (created / submitted at created_at, filled and observed at entry_at), its order events (raw filled_at,
    or `entry_raw` / `exit_raw` as given), optionally a filled exit intent (submitted at exit_submitted_at, default exit_at - 60 s; observed at
    exit_at) and the CLOSED position with a pnl snapshot; marks are (at, last_price, last_price_at)."""
    pid, xid, bid = uuid.uuid4().hex, uuid.uuid4().hex, uuid.uuid4().hex
    created = iso(created_at or (entry_at - timedelta(seconds=30)))
    realized = realized if realized is not None else (None if exit_px is None else f"{(float(exit_px) - float(entry_px)) * qty:.2f}")
    closed = exit_at is not None
    conn = sqlite3.connect(str(db))
    try:
        conn.execute("INSERT INTO execution_intents (execution_id, intent_type, symbol, side, qty, client_order_id, position_id, state, broker_order_id, filled_qty, filled_avg_price, "
                     "attempt_count, created_at, submitted_at, resolved_at, updated_at) VALUES (?, 'ENTRY', ?, 'buy', ?, ?, ?, 'FILLED', ?, ?, ?, 1, ?, ?, ?, ?)",
                     (xid, symbol, qty, f"{coid_prefix}{xid[:12]}", pid, bid, qty, entry_px, created, created, iso(entry_at), iso(entry_at)))
        conn.execute("INSERT INTO execution_order_events (event_id, broker_order_id, execution_id, at, status, filled_qty, filled_avg_price, source, raw_json) VALUES (?,?,?,?,?,?,?,?,?)",
                     (uuid.uuid4().hex, bid, xid, iso(entry_at), "filled", qty, entry_px, "poll", json.dumps(entry_raw if entry_raw is not None else {"filled_at": iso(filled_at or entry_at)})))
        snap = {"entry_qty": qty, "average_entry_price": entry_px, "exit_qty": 0, "average_exit_price": None, "realized_pnl": "0.00"}
        conn.execute("INSERT INTO position_events (event_id, position_id, at, kind, from_state, to_state, reason, detail_json) VALUES (?,?,?,?,?,?,?,?)",
                     (uuid.uuid4().hex, pid, iso(entry_at), "OPENED", None, "HEALTHY", "entry filled", json.dumps({"pnl_snapshot": snap})))
        if closed:
            eid, ebid = uuid.uuid4().hex, uuid.uuid4().hex
            conn.execute("INSERT INTO execution_intents (execution_id, intent_type, symbol, side, qty, client_order_id, position_id, exit_cycle, state, broker_order_id, filled_qty, "
                         "filled_avg_price, attempt_count, created_at, submitted_at, resolved_at, updated_at) VALUES (?, 'EXIT', ?, 'sell', ?, ?, ?, 1, 'FILLED', ?, ?, ?, 1, ?, ?, ?, ?)",
                         (eid, symbol, qty, f"{coid_prefix}{eid[:12]}", pid, ebid, qty, exit_px, iso(exit_submitted_at or (exit_at - timedelta(seconds=60))),
                          iso(exit_submitted_at or (exit_at - timedelta(seconds=60))), iso(exit_at), iso(exit_at)))
            conn.execute("INSERT INTO execution_order_events (event_id, broker_order_id, execution_id, at, status, filled_qty, filled_avg_price, source, raw_json) VALUES (?,?,?,?,?,?,?,?,?)",
                         (uuid.uuid4().hex, ebid, eid, iso(exit_at), "filled", qty, exit_px, "poll", json.dumps(exit_raw if exit_raw is not None else {"filled_at": iso(exit_filled_at or exit_at)})))
            snap = {"entry_qty": qty, "average_entry_price": entry_px, "exit_qty": qty, "average_exit_price": exit_px, "realized_pnl": realized}
            conn.execute("INSERT INTO position_events (event_id, position_id, at, kind, from_state, to_state, reason, detail_json) VALUES (?,?,?,?,?,?,?,?)",
                         (uuid.uuid4().hex, pid, iso(exit_at), "CLOSED", "EXITING", "CLOSED", "exit filled", json.dumps({"pnl_snapshot": snap})))
        conn.execute("INSERT INTO paper_positions (position_id, execution_id, symbol, quantity, entry_qty, average_entry_price, opened_at, state, exit_qty, average_exit_price, realized_pnl, "
                     "closed_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                     (pid, xid, symbol, 0 if closed else qty, qty, entry_px, iso(entry_at), "CLOSED" if closed else "HEALTHY", qty if closed else 0, exit_px if closed else None,
                      realized if closed else None, iso(exit_at) if closed else None, iso(exit_at or entry_at)))
        for at, px, lpa in marks:
            conn.execute("INSERT INTO forward_validation_marks (mark_id, at, position_id, symbol, state, quantity, last_price, last_price_at) VALUES (?,?,?,?,?,?,?,?)",
                         (uuid.uuid4().hex, iso(at), pid, symbol, "HEALTHY", qty, px, iso(lpa) if lpa is not None else None))
        if snapshot_cohort is not None:
            conn.execute("INSERT INTO forward_capture_entry_snapshots (execution_id, candidate_id, decision_id, cohort_id, captured_at, snapshot_json, snapshot_sha256) VALUES (?,?,?,?,?,?,?)",
                         (xid, None, None, snapshot_cohort, iso(entry_at), "{}", "s" * 64))
        conn.commit()
    finally:
        conn.close()
    return {"position_id": pid, "execution_id": xid}
