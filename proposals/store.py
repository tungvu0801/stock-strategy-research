"""
proposals/store.py — SQLite persistence for review proposals (DESIGN_59 §10–§11). A generated proposal, its control rows and the
control status upserts are written in ONE transaction; nothing is reported as persisted unless the commit succeeded. Reads open
their own read-only connection and tolerate a database without the Stage 5.9 tables. Only review_state / reviewed_at ever change.
"""
from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from database.proposal_migrations import run_proposal_migrations
from fit import readonly as RO
from proposals import STATUS_LABELS
from proposals.engine import proposal_id_for
from proposals.models import ControlEvaluation, Draft

JSON_COLS = ("blockers_json", "warnings_json", "inputs_json", "controls_json", "ppe_snapshot_json", "demand_snapshot_json", "evidence_snapshot_json", "setup_snapshot_json")


class ProposalStoreError(RuntimeError):
    pass


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds")


def _json(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), default=str)


def new_id() -> str:
    return uuid.uuid4().hex


def decode(row: Optional[dict]) -> Optional[dict]:
    """A stored row with its JSON columns decoded (blockers, warnings, inputs, controls, snapshots)."""
    if row is None:
        return None
    out = dict(row)
    for col in JSON_COLS:
        key = col[:-5]
        try:
            out[key] = json.loads(out.pop(col) or ("[]" if key in ("blockers", "warnings", "controls") else "{}"))
        except (TypeError, ValueError):
            out[key] = [] if key in ("blockers", "warnings", "controls") else {}
    out["status_label"] = STATUS_LABELS.get(out.get("status"), out.get("status"))
    out["caps"] = dict((out.get("inputs") or {}).get("caps") or {})
    return out


class ProposalStore:
    def __init__(self, path: Optional[Path] = None):
        self.path = Path(path) if path else RO.db_path()

    # ---------------------------------------------------------------- connections -------------------------------------------
    def _rw(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.path), timeout=10.0, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        run_proposal_migrations(conn)
        return conn

    def _ro(self) -> Optional[sqlite3.Connection]:
        if not self.path.exists():
            return None
        conn = sqlite3.connect(f"file:{self.path.as_posix()}?mode=ro", uri=True, timeout=10.0)
        conn.row_factory = sqlite3.Row
        try:
            names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table' AND name IN ('trade_proposals', 'proposal_controls', 'proposal_control_status')")}
        except sqlite3.Error:
            conn.close()
            return None
        if "trade_proposals" not in names:
            conn.close()
            return None
        return conn

    def _read(self, sql: str, args=()) -> List[dict]:
        conn = self._ro()
        if conn is None:
            return []
        try:
            return [dict(r) for r in conn.execute(sql, args).fetchall()]
        finally:
            conn.close()

    # ---------------------------------------------------------------- reads ----------------------------------------------------
    def get(self, proposal_id: str) -> Optional[dict]:
        rows = self._read("SELECT * FROM trade_proposals WHERE proposal_id = ?", (proposal_id,))
        return decode(rows[0]) if rows else None

    def latest(self, symbol: str) -> Optional[dict]:
        rows = self._read("SELECT * FROM trade_proposals WHERE symbol = ? ORDER BY created_at DESC, rowid DESC LIMIT 1", (symbol.upper(),))
        return decode(rows[0]) if rows else None

    def latest_for_setup(self, setup_id: str) -> Optional[dict]:
        rows = self._read("SELECT * FROM trade_proposals WHERE setup_id = ? ORDER BY created_at DESC, rowid DESC LIMIT 1", (setup_id,))
        return decode(rows[0]) if rows else None

    def latest_per_symbol(self) -> List[dict]:
        rows = self._read("SELECT p.* FROM trade_proposals p JOIN (SELECT symbol, MAX(rowid) AS rid FROM trade_proposals GROUP BY symbol) m ON m.rid = p.rowid ORDER BY p.symbol")
        return [decode(r) for r in rows]

    def history(self, symbol: Optional[str] = None, limit: int = 50) -> List[dict]:
        if symbol:
            rows = self._read("SELECT * FROM trade_proposals WHERE symbol = ? ORDER BY created_at DESC, rowid DESC LIMIT ?", (symbol.upper(), limit))
        else:
            rows = self._read("SELECT * FROM trade_proposals ORDER BY created_at DESC, rowid DESC LIMIT ?", (limit,))
        return [decode(r) for r in rows]

    def count(self, symbol: Optional[str] = None) -> int:
        rows = self._read("SELECT count(*) AS n FROM trade_proposals" + (" WHERE symbol = ?" if symbol else ""), (symbol.upper(),) if symbol else ())
        return int(rows[0]["n"]) if rows else 0

    def controls_for(self, proposal_id: str) -> List[dict]:
        rows = self._read("SELECT * FROM proposal_controls WHERE proposal_id = ? ORDER BY rowid", (proposal_id,))
        for r in rows:
            try:
                r["input"] = json.loads(r.pop("input_json") or "{}")
            except (TypeError, ValueError):
                r["input"] = {}
        return rows

    def control_evaluations(self, control_id: Optional[str] = None, limit: int = 50) -> List[dict]:
        if control_id:
            rows = self._read("SELECT * FROM proposal_controls WHERE control_id = ? ORDER BY evaluated_at DESC, rowid DESC LIMIT ?", (control_id, limit))
        else:
            rows = self._read("SELECT * FROM proposal_controls ORDER BY evaluated_at DESC, rowid DESC LIMIT ?", (limit,))
        for r in rows:
            try:
                r["input"] = json.loads(r.pop("input_json") or "{}")
            except (TypeError, ValueError):
                r["input"] = {}
        return rows

    def control_status(self) -> List[dict]:
        rows = self._read("SELECT * FROM proposal_control_status ORDER BY control_id")
        for r in rows:
            try:
                r["last_input"] = json.loads(r.pop("last_input_json") or "{}")
            except (TypeError, ValueError):
                r["last_input"] = {}
        return rows

    # ---------------------------------------------------------------- writes ---------------------------------------------------
    @staticmethod
    def _touch(conn: sqlite3.Connection, c: ControlEvaluation, now_iso: str, symbol: str, proposal_id: Optional[str]) -> None:
        blocked = c.result == "BLOCK"
        conn.execute("INSERT INTO proposal_control_status (control_id, last_evaluated_at, last_result, last_reason, last_symbol, last_proposal_id, evaluation_count, block_count, "
                     "last_blocked_at, last_input_json, updated_at) VALUES (?,?,?,?,?,?,1,?,?,?,?) ON CONFLICT(control_id) DO UPDATE SET last_evaluated_at = excluded.last_evaluated_at, "
                     "last_result = excluded.last_result, last_reason = excluded.last_reason, last_symbol = excluded.last_symbol, last_proposal_id = excluded.last_proposal_id, "
                     "evaluation_count = evaluation_count + 1, block_count = block_count + excluded.block_count, last_blocked_at = COALESCE(excluded.last_blocked_at, last_blocked_at), "
                     "last_input_json = excluded.last_input_json, updated_at = excluded.updated_at",
                     (c.control_id, now_iso, c.result, c.reason, symbol, proposal_id, 1 if blocked else 0, now_iso if blocked else None, _json(c.input), now_iso))

    def touch_controls(self, controls: List[ControlEvaluation], now: datetime, symbol: str, proposal_id: Optional[str]) -> None:
        """Liveness only (the reuse path): the status rows record that the controls ran again, without a new proposal row."""
        conn = self._rw()
        try:
            conn.execute("BEGIN IMMEDIATE")
            for c in controls:
                self._touch(conn, c, _iso(now), symbol.upper(), proposal_id)
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
        finally:
            conn.close()

    def insert(self, draft: Draft, now: datetime, trigger: str, owner: str) -> dict:
        """ONE transaction: the proposal row, one control row per control, the control status upserts. Returns the decoded row."""
        if draft.status not in ("BLOCKED", "REVIEWABLE") or not draft.setup_id:
            raise ProposalStoreError("only BLOCKED or REVIEWABLE proposals of an open setup are stored")
        stamp = _iso(now)
        pid = proposal_id_for(draft.setup_id, stamp, draft.reference_price, draft.invalidation_price, draft.risk_budget)
        conn = self._rw()
        hit = conn.execute("SELECT fingerprint FROM trade_proposals WHERE proposal_id = ?", (pid,)).fetchone()
        if hit is not None:                                                   # same setup, second, price, stop and budget: the same identity
            conn.close()
            if hit["fingerprint"] == draft.fingerprint:
                return self.get(pid)                                          # identical snapshot: that row IS this proposal
            raise ProposalStoreError("a different proposal with the same identity was stored in this second; evaluate again a second later")
        try:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute("INSERT INTO trade_proposals (proposal_id, setup_id, symbol, setup_type, side_context, status, created_at, expires_at, price_mode, reference_price, price_source, "
                         "price_timestamp, invalidation_price, stop_rule, structural_low, atr, atr_period, risk_per_share, risk_pct, equity, equity_source, risk_fraction, risk_budget, "
                         "base_qty, position_cap_qty, buying_power_qty, final_qty, max_notional, max_position_pct, cap_source, existing_position_qty, existing_position_notional, "
                         "existing_avg_cost, proposed_notional, blockers_json, warnings_json, inputs_json, controls_json, ppe_snapshot_json, demand_snapshot_json, evidence_snapshot_json, "
                         "setup_snapshot_json, review_state, reviewed_at, trigger, owner, source, mode, fingerprint) VALUES "
                         "(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,'UNREVIEWED',NULL,?,?,?,?,?)",
                         (pid, draft.setup_id, draft.symbol, draft.setup_type, "LONG_REVIEW", draft.status, stamp, draft.expires_at, draft.price_mode, draft.reference_price,
                          draft.price_source, draft.price_timestamp, draft.invalidation_price, draft.stop_rule, draft.structural_low, draft.atr, int(draft.atr_period), draft.risk_per_share,
                          draft.risk_pct, draft.equity, draft.equity_source, draft.risk_fraction, draft.risk_budget, draft.base_qty, draft.position_cap_qty, draft.buying_power_qty,
                          draft.final_qty, draft.max_notional, draft.max_position_pct, draft.cap_source, draft.existing_position_qty, draft.existing_position_notional, draft.existing_avg_cost,
                          draft.proposed_notional, _json(draft.blockers), _json(draft.warnings), _json(draft.inputs), _json([c.model_dump() for c in draft.controls]),
                          _json(draft.ppe_snapshot), _json(draft.demand_snapshot), _json(draft.evidence_snapshot), _json(draft.setup_snapshot), trigger, owner, draft.source, draft.mode,
                          draft.fingerprint))
            for c in draft.controls:
                conn.execute("INSERT INTO proposal_controls (control_eval_id, proposal_id, control_id, symbol, evaluated_at, result, reason, input_json) VALUES (?,?,?,?,?,?,?,?)",
                             (new_id(), pid, c.control_id, draft.symbol, stamp, c.result, c.reason, _json(c.input)))
                self._touch(conn, c, stamp, draft.symbol, pid)
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
        finally:
            conn.close()
        return self.get(pid)

    def set_review(self, proposal_id: str, state: str, now: datetime) -> Optional[dict]:
        """VIEWED (only from UNREVIEWED) or DISMISSED (from any state but DISMISSED). Nothing else ever changes on a stored row."""
        if state not in ("VIEWED", "DISMISSED"):
            raise ProposalStoreError("review state must be VIEWED or DISMISSED")
        conn = self._rw()
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT review_state FROM trade_proposals WHERE proposal_id = ?", (proposal_id,)).fetchone()
            if row is None:
                conn.execute("ROLLBACK")
                return None
            current = row["review_state"]
            allowed = (state == "VIEWED" and current == "UNREVIEWED") or (state == "DISMISSED" and current != "DISMISSED")
            if allowed:
                conn.execute("UPDATE trade_proposals SET review_state = ?, reviewed_at = ? WHERE proposal_id = ?", (state, _iso(now), proposal_id))
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
        finally:
            conn.close()
        return self.get(proposal_id)
