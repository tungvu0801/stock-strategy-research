"""
Bot-position recovery (BOT_POSITION_RECOVERY_DESIGN.md revision 3.5, section 9; the operator's decisions R-1 to R-5 of 2026-11-09; Task AD): the
local command execution/recovery.py, the executor's reconcile while unhealthy with nothing open, the MANUAL_EXTERNAL exit text, the intervention
note (forward validation's interventions(), the daily report's NOTES, the live attention codes) and forward capture's skip. Every test runs on a
scratch SQLite file, with rows written directly or with the Stage 6.0 simulated paper broker under the real stream wiring: no socket, no broker
credential, no real order.
  C   one test per precondition (C01-C12), each refusing with its code and writing nothing, and the other refusals (C13-C19)
  A   apply: the rows a close and a partial write, the safeguards S1-S4 and every output sentence
  R   races on a real SQLite file with separate connections
  E   end to end on the shared 6.10A rig (executor, account refresh, cash flow, gate, forward validation) with the simulated paper broker
  V   the intervention note and the evidence (forward_validation, reports, forward_capture)
  T   the task's scenarios: matching startup, restart, unmanaged positions, open and partial orders, timeouts, stale data, an inconsistent
      database, repeated and concurrent requests, Emergency Stop and the hold states
  S   static pins
"""
import ast
import hashlib
import json
import sqlite3
import threading
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest

from test_cash_flow_602 import cash_config  # noqa: F401 - the shared rig's autouse config fixture (and its wire tripwire)
import wave2_pra_support as W2
import config
import execution as X
from account_refresh.store import AccountRefreshStore
from database.account_refresh_migrations import run_account_refresh_migrations
from database.alpaca_order_migrations import run_alpaca_order_migrations
from database.execution_migrations import run_execution_migrations
from execution import recovery as R
from execution.service import ExecutionRefused, ExecutionService
from execution.store import ExecutionStore
from forward_validation import CRITICAL_CODES
from forward_validation import reports as REP
from forward_validation.service import ForwardValidationService, ny_window
from forward_validation.store import ForwardValidationStore

ROOT = Path(__file__).resolve().parents[1]
UTC = timezone.utc
NOW = datetime(2026, 10, 21, 15, 0, tzinfo=UTC)                         # Wednesday 11:00 New York: the regular session
OFF = datetime(2026, 10, 21, 2, 0, tzinfo=UTC)                          # Tuesday 22:00 New York: outside it
PID = "pos-1"
CHECK_TEXT = "Confirm on Alpaca's website that SNDK is flat. If you hold shares, sell them there: the app has no record of them and they have no automatic exit."


def iso(dt):
    return dt.astimezone(UTC).isoformat(timespec="seconds")


def content(path):
    return W2.content_hash(Path(path))[0]


# ================================================================ the scratch database (rows written directly) ============================
class Lab:
    """The rows a recoverable bot position has: an ATTENTION position, its latest finding, a fresh account snapshot with its attempt and
    liveness, and a live executor run row. Each is written by its own helper, so one test perturbs one precondition."""

    def __init__(self, tmp_path, now=NOW, path=None):
        self.path = Path(path) if path else Path(tmp_path) / "recovery.db"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.now = now
        c = sqlite3.connect(str(self.path))
        run_execution_migrations(c)
        run_account_refresh_migrations(c)
        c.close()

    def sql(self, q, args=()):
        c = sqlite3.connect(str(self.path), timeout=10)
        try:
            c.execute(q, args)
            c.commit()
        finally:
            c.close()

    def rows(self, q, args=()):
        c = sqlite3.connect(str(self.path), timeout=10)
        c.row_factory = sqlite3.Row
        try:
            return [dict(r) for r in c.execute(q, args)]
        finally:
            c.close()

    def at(self, s):
        return iso(self.now + timedelta(seconds=s))

    def position(self, pid=PID, sym="SNDK", qty=5, reason="BROKER_FLAT_INTERNAL_OPEN", state="ATTENTION", exit_reason=None, exit_cycle=0, exit_qty=0,
                 realized_pnl=None, updated=-300, entry_qty=None):
        self.sql("INSERT INTO paper_positions (position_id, execution_id, symbol, quantity, entry_qty, average_entry_price, opened_at, state, attention_reason, exit_reason, "
                 "exit_cycle, exit_qty, realized_pnl, updated_at) VALUES (?,?,?,?,?,'100.00',?,?,?,?,?,?,?,?)",
                 (pid, "x-" + pid, sym, qty, entry_qty or max(qty + exit_qty, 1), self.at(-3600), state, reason, exit_reason, exit_cycle, exit_qty, realized_pnl, self.at(updated)))
        return self

    def finding(self, pid=PID, sym="SNDK", kind="BROKER_FLAT_INTERNAL_OPEN", detail=None, at=-290):
        self.sql("INSERT INTO execution_reconciliations (reconciliation_id, run_id, at, trigger, kind, symbol, blocking, position_id, detail_json) VALUES (?,?,?,?,?,?,1,?,?)",
                 (uuid.uuid4().hex, None, self.at(at), "TEST", kind, sym, pid, json.dumps(detail if detail is not None else {"internal_qty": 5})))
        return self

    def snapshot(self, positions=(), orders=(), refreshed=-30, started=None, failures=0):
        st = AccountRefreshStore(self.path)
        rid = uuid.uuid4().hex
        when = self.now + timedelta(seconds=refreshed)
        snap = {"refresh_id": rid, "refreshed_at": iso(when), "source": "SIMULATED_PAPER", "host": "paper-api.alpaca.markets", "account": {"status": "ACTIVE"},
                "positions": list(positions), "open_orders": list(orders)}
        st.commit_success(snap, {"started_at": self.at(started if started is not None else refreshed), "finished_at": iso(when), "trigger": "TEST", "result": "SUCCESS",
                                 "read_attempts": 1, "refresh_id": rid, "owner": "test"}, when)
        for _ in range(failures):
            st.commit_failure({"started_at": self.at(refreshed + 1), "finished_at": self.at(refreshed + 1), "trigger": "TEST", "result": "FAILED", "read_attempts": 1,
                               "error_code": "HTTP_500", "error_text": "", "owner": "test"}, self.now, 1.0)
        return self

    def run(self, started=-20, result="RUN", error=None):
        self.sql("INSERT INTO execution_runs (run_id, started_at, finished_at, trigger, mode, result, summary_json, error, owner) VALUES (?,?,?,?,?,?,'{}',?,?)",
                 (uuid.uuid4().hex, self.at(started), self.at(started + 1), "SCHEDULED", "MONITOR_ONLY", result, error, "test"))
        return self

    def mode(self, mode):
        self.sql("INSERT INTO execution_mode (id, mode, changed_at, changed_by, reason, environment_json) VALUES (1, ?, ?, 'test', NULL, '{}') "
                 "ON CONFLICT(id) DO UPDATE SET mode = excluded.mode", (mode, self.at(-900)))
        return self

    def intent(self, sym="SNDK", state="SUBMITTED", kind="EXIT", pid=PID):
        n = uuid.uuid4().hex
        self.sql("INSERT INTO execution_intents (execution_id, intent_type, symbol, side, qty, client_order_id, position_id, state, created_at, updated_at) "
                 "VALUES (?,?,?,?,1,?,?,?,?,?)", (n, kind, sym, "sell" if kind == "EXIT" else "buy", "stockagent-test-" + n, pid if kind == "EXIT" else None, state,
                                                   self.at(-60), self.at(-60)))
        return n

    def manual(self, sym="SNDK", state="SUBMITTED", event_at=None):
        """A Stage 4.6B intent (its tables are created here, as the first Link creates them) and optionally one event."""
        c = sqlite3.connect(str(self.path))
        run_alpaca_order_migrations(c)
        c.close()
        n = uuid.uuid4().hex
        h = lambda s: hashlib.sha256(s.encode()).hexdigest()  # noqa: E731
        self.sql("INSERT INTO alpaca_paper_order_intents (client_order_id, symbol, side, qty, order_type, time_in_force, payload_json, payload_sha256, preview_json, preview_hash, "
                 "account_fp, account_masked, previewed_at, expires_at, state, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                 ("sa46b-" + n, sym, "sell", 1, "market", "day", "{}", h("p" + n), "{}", h("v" + n), h("a"), "****0001", self.at(-900), self.at(-780), state, self.at(-900)))
        iid = self.rows("SELECT intent_id FROM alpaca_paper_order_intents WHERE client_order_id = ?", ("sa46b-" + n,))[0]["intent_id"]
        if event_at is not None:
            self.sql("INSERT INTO alpaca_paper_order_events (intent_id, at, kind) VALUES (?, ?, 'STATUS_CHANGE')", (iid, self.at(event_at)))
        return iid

    def close_case(self, **pos):
        """Recoverable: the broker is flat (BROKER_FLAT_INTERNAL_OPEN, 5 shares)."""
        return self.position(**pos).finding().snapshot().run()

    def partial_case(self, broker=2, **pos):
        """Recoverable: the broker holds 2 of 5 (QTY_MISMATCH)."""
        pos.setdefault("reason", "QTY_MISMATCH")
        return self.position(**pos).finding(kind="QTY_MISMATCH", detail={"internal_qty": 5, "broker_qty": broker}).snapshot(
            positions=[{"symbol": "SNDK", "qty": str(broker), "side": "long"}]).run()

    def preview(self, pid=PID, now=None):
        return R.preview(self.path, pid, now or self.now)

    def apply(self, h=None, confirmed=True, pid=PID, now=None, who="operator", why="sold on Alpaca's website", **kw):
        h = h if h is not None else self.preview(pid, now)["evidence_hash"]
        return R.apply(self.path, pid, h, who, why, confirmed, now or self.now, **kw)

    def pos(self, pid=PID):
        return self.rows("SELECT * FROM paper_positions WHERE position_id = ?", (pid,))[0]

    def events(self, pid=PID):
        out = self.rows("SELECT kind, from_state, to_state, reason, detail_json, at FROM position_events WHERE position_id = ? ORDER BY rowid", (pid,))
        for e in out:
            e["detail"] = json.loads(e.pop("detail_json"))
        return out


def refused(lab, code, pid=PID, **kw):
    """preview and apply both refuse with `code`, and nothing is written."""
    before = content(lab.path)
    pv = lab.preview(pid)
    assert pv["ok"] is False and code in pv["refusals"], (code, pv["refusals"], pv["preconditions"])
    out = lab.apply(h=pv["evidence_hash"] or "0" * 64, pid=pid, **kw)
    assert out["ok"] is False and out["written"] is False and code in out["refusals"], (code, out)
    assert content(lab.path) == before
    return pv


def check(pv, n):
    return next(c for c in pv["preconditions"] if c["n"] == n)


# ================================================================ C the twelve preconditions ===============================================
def test_C01_not_recoverable_unknown_id_healthy_closed_or_another_attention_reason(tmp_path):
    lab = Lab(tmp_path).close_case()
    pv = refused(lab, "NOT_RECOVERABLE", pid="no-such-position")
    assert pv["refusals"] == ["NOT_RECOVERABLE"] and pv["evidence_hash"] is None
    for i, (state, reason) in enumerate((("HEALTHY", None), ("CLOSED", None), ("EXIT_SUBMITTED", None), ("ATTENTION", None), ("ATTENTION", "MANUAL_REVIEW"))):
        lab.position(pid=f"p{i}", state=state, reason=reason)
        lab.finding(pid=f"p{i}")
        pv = refused(lab, "NOT_RECOVERABLE", pid=f"p{i}")
        assert "recoverable: ATTENTION" in check(pv, 1)["detail"], state
    for reason in R.RECOVERABLE_REASONS:
        assert reason in ("BROKER_FLAT_INTERNAL_OPEN", "QTY_MISMATCH", "EXIT_ATTEMPTS_EXHAUSTED")
    assert lab.preview()["ok"] is True


def test_C02_paper_auto_on_refuses(tmp_path):
    lab = Lab(tmp_path).close_case().mode("PAPER_AUTO")
    pv = refused(lab, "PAPER_AUTO_ON")
    assert pv["mode"] == "PAPER_AUTO" and pv["refusals"] == ["PAPER_AUTO_ON"]
    lab.mode("MONITOR_ONLY")
    assert lab.preview()["ok"] is True


def test_C03_an_open_execution_intent_for_the_position_or_the_symbol_refuses(tmp_path):
    for state in sorted(X.INTENT_OPEN):
        for kind in ("EXIT", "ENTRY"):
            lab = Lab(tmp_path / f"{state}-{kind}").close_case()
            lab.intent(state=state, kind=kind)
            assert refused(lab, "INTENT_OPEN")["refusals"] == ["INTENT_OPEN"], (state, kind)
    lab = Lab(tmp_path / "terminal").close_case()
    for state in ("FILLED", "CANCELED", "REJECTED", "NOT_SENT", "FAILED"):
        lab.intent(state=state)
    lab.intent(sym="AMD", state="SUBMITTED", kind="ENTRY")                        # another symbol's open intent does not concern this position
    assert lab.preview()["ok"] is True


def test_C04_a_live_or_unresolved_manual_order_or_a_manual_event_after_the_snapshot_refuses_and_absent_tables_read_as_none(tmp_path):
    assert Lab(tmp_path / "absent").close_case().preview()["ok"] is True        # 4.6B never linked: its tables are absent, read as no intents
    for state in sorted(R.MANUAL_INTENT_STATES):
        lab = Lab(tmp_path / state).close_case()
        lab.manual(state=state)
        assert refused(lab, "MANUAL_INTENT_ACTIVE")["refusals"] == ["MANUAL_INTENT_ACTIVE"], state
    assert set(R.MANUAL_INTENT_STATES) == {"CONFIRMING", "SUBMISSION_PENDING", "SUBMIT_NOT_SENT", "RECONCILIATION_REQUIRED", "SUBMITTED", "BROKER_ACCEPTED", "PARTIALLY_FILLED"}
    lab = Lab(tmp_path / "event").close_case()
    lab.manual(state="FILLED", event_at=-5)                                       # a terminal manual order with an event after the snapshot's attempt began
    assert refused(lab, "MANUAL_INTENT_ACTIVE")["refusals"] == ["MANUAL_INTENT_ACTIVE"]
    lab = Lab(tmp_path / "same").close_case()
    lab.manual(state="FILLED", event_at=-30)                                      # in the very second the attempt began (whole-second stamps): refused
    assert refused(lab, "MANUAL_INTENT_ACTIVE")["refusals"] == ["MANUAL_INTENT_ACTIVE"]
    lab = Lab(tmp_path / "older").close_case()
    lab.manual(state="FILLED", event_at=-200)                                     # its last event before the attempt: allowed
    lab.manual(sym="AMD", state="SUBMITTED", event_at=-5)                        # another symbol: allowed
    assert lab.preview()["ok"] is True


def test_C05_no_snapshot_a_refresh_failure_or_an_unreadable_snapshot_refuses(tmp_path):
    lab = Lab(tmp_path / "none").position().finding().run()
    assert refused(lab, "SNAPSHOT_NOT_TRUSTED")
    lab = Lab(tmp_path / "failed").position().finding().snapshot(failures=1).run()
    pv = refused(lab, "SNAPSHOT_NOT_TRUSTED")
    assert "1 consecutive failure" in check(pv, 5)["detail"]
    lab = Lab(tmp_path / "bad").close_case()
    lab.sql("UPDATE account_refresh_snapshot SET positions_json = 'not json' WHERE id = 1")
    assert refused(lab, "SNAPSHOT_NOT_TRUSTED")
    lab = Lab(tmp_path / "nolive").close_case()
    lab.sql("DROP TRIGGER account_refresh_liveness_no_delete")                   # a damaged database (only a hand edit could do this)
    lab.sql("DELETE FROM account_refresh_liveness")
    assert refused(lab, "SNAPSHOT_NOT_TRUSTED")


def test_C06_the_snapshot_age_two_refresh_intervals_for_a_close_600_s_for_a_partial(tmp_path):
    assert config.ACCOUNT_REFRESH_SECONDS == 60 and config.ACCOUNT_REFRESH_IDLE_SECONDS == 300
    for now, ok_age, limit in ((NOW, 120, 120), (OFF, 600, 600)):                 # a close: 2 x 60 s in session, 2 x 300 s outside
        lab = Lab(tmp_path / f"c{limit}", now=now).position(updated=-900).finding(at=-890).snapshot(refreshed=-ok_age).run()
        assert lab.preview()["ok"] is True
        lab2 = Lab(tmp_path / f"c{limit}x", now=now).position(updated=-900).finding(at=-890).snapshot(refreshed=-(ok_age + 1)).run()
        pv = refused(lab2, "SNAPSHOT_TOO_OLD")
        assert f"limit {limit} s" in check(pv, 6)["detail"]
    lab = Lab(tmp_path / "p").position(reason="QTY_MISMATCH", updated=-900).finding(kind="QTY_MISMATCH", detail={"internal_qty": 5, "broker_qty": 2}, at=-890).snapshot(
        positions=[{"symbol": "SNDK", "qty": "2", "side": "long"}], refreshed=-600).run()
    assert lab.preview()["ok"] is True and lab.preview()["case"] == "PARTIAL"     # a partial: 600 s, in session too
    lab = Lab(tmp_path / "px").position(reason="QTY_MISMATCH", updated=-900).finding(kind="QTY_MISMATCH", detail={"internal_qty": 5, "broker_qty": 2}, at=-890).snapshot(
        positions=[{"symbol": "SNDK", "qty": "2", "side": "long"}], refreshed=-601).run()
    refused(lab, "SNAPSHOT_TOO_OLD")
    lab = Lab(tmp_path / "future").close_case()
    lab.now = NOW - timedelta(seconds=60)                                          # the clock set back: the snapshot is in the future
    pv = lab.preview()
    assert "SNAPSHOT_TOO_OLD" in pv["refusals"] and "in the future" in check(pv, 6)["detail"]


def test_C07_the_snapshot_attempt_must_start_after_the_position_update_and_its_latest_finding(tmp_path):
    lab = Lab(tmp_path / "upd").position(updated=-20).finding(at=-300).snapshot(refreshed=-30).run()
    refused(lab, "SNAPSHOT_OLDER_THAN_FINDING")
    lab = Lab(tmp_path / "fnd").position(updated=-300).finding(at=-10).snapshot(refreshed=-30).run()
    refused(lab, "SNAPSHOT_OLDER_THAN_FINDING")
    lab = Lab(tmp_path / "same").position(updated=-300).finding(at=-30).snapshot(refreshed=-30).run()
    refused(lab, "SNAPSHOT_OLDER_THAN_FINDING")                                   # the same second is not after
    lab = Lab(tmp_path / "ok").position(updated=-300).finding(at=-31).snapshot(refreshed=-30).run()
    assert lab.preview()["ok"] is True


def test_C08_an_open_order_in_the_symbol_or_a_full_order_list_refuses(tmp_path):
    lab = Lab(tmp_path / "mine").position().finding().snapshot(orders=[{"id_short": "aaaa", "symbol": "SNDK", "side": "buy", "status": "new"}]).run()
    refused(lab, "OPEN_ORDER_IN_SYMBOL")
    others = [{"id_short": f"{i:04d}", "symbol": "AMD", "side": "buy", "status": "new"} for i in range(500)]
    lab = Lab(tmp_path / "full").position().finding().snapshot(orders=others).run()
    pv = refused(lab, "OPEN_ORDER_IN_SYMBOL")
    assert "may be cut" in check(pv, 8)["detail"]
    lab = Lab(tmp_path / "ok").position().finding().snapshot(orders=others[:499]).run()
    assert lab.preview()["ok"] is True


def test_C09_more_shares_than_the_record_equal_a_short_a_fraction_or_two_rows_refuse(tmp_path):
    for name, positions in (("above", [{"symbol": "SNDK", "qty": "6", "side": "long"}]), ("equal", [{"symbol": "SNDK", "qty": "5", "side": "long"}]),
                            ("short", [{"symbol": "SNDK", "qty": "-2", "side": "short"}]), ("fraction", [{"symbol": "SNDK", "qty": "2.5", "side": "long"}]),
                            ("two", [{"symbol": "SNDK", "qty": "1", "side": "long"}, {"symbol": "SNDK", "qty": "1", "side": "long"}]),
                            ("nan", [{"symbol": "SNDK", "qty": "x", "side": "long"}])):
        lab = Lab(tmp_path / name).position(reason="QTY_MISMATCH").finding(kind="QTY_MISMATCH", detail={"internal_qty": 5, "broker_qty": 2}).snapshot(positions=positions).run()
        pv = refused(lab, "BROKER_QTY_NOT_RECOVERABLE")
        assert pv["case"] is None and pv["evidence_hash"] is None, name
    lab = Lab(tmp_path / "zero").position().finding().snapshot(positions=[{"symbol": "SNDK", "qty": "0", "side": "long"}]).run()
    assert lab.preview()["case"] == "CLOSE" and lab.preview()["ok"] is True          # a zero row reads as flat


def test_C10_the_latest_finding_must_agree_with_the_snapshot(tmp_path):
    lab = Lab(tmp_path / "dis").position(reason="QTY_MISMATCH").finding(kind="QTY_MISMATCH", detail={"internal_qty": 5, "broker_qty": 3}).snapshot(
        positions=[{"symbol": "SNDK", "qty": "2", "side": "long"}]).run()
    pv = refused(lab, "FINDING_DISAGREES")
    assert check(pv, 10)["detail"].endswith("Wait for the next cycle. If it still refuses, restart the server.")
    lab = Lab(tmp_path / "none").position().snapshot().run()
    refused(lab, "FINDING_DISAGREES")
    lab = Lab(tmp_path / "pred").position().finding(at=-295).finding(kind="QTY_MISMATCH", detail={"internal_qty": 5, "broker_qty": 2}, at=-290).snapshot().run()
    refused(lab, "FINDING_DISAGREES")                                             # the latest finding counts, never its predecessor
    lab = Lab(tmp_path / "flat0").position(reason="QTY_MISMATCH").finding(detail={"internal_qty": 5, "broker_qty": 0}).snapshot().run()
    assert lab.preview()["ok"] is True                                             # _maybe_submit_exit's flat finding carries broker_qty 0
    lab = Lab(tmp_path / "exh").position(reason="EXIT_ATTEMPTS_EXHAUSTED", exit_cycle=3, updated=-300).finding(at=-310).snapshot().run()
    refused(lab, "FINDING_DISAGREES")                                             # EXHAUSTED: the finding must be dated at or after updated_at
    lab = Lab(tmp_path / "exh2").position(reason="EXIT_ATTEMPTS_EXHAUSTED", exit_cycle=3, updated=-300).finding(at=-300).snapshot().run()
    assert lab.preview()["ok"] is True
    lab = Lab(tmp_path / "kind").position().finding(kind="MULTIPLE_ORDERS", detail={"count": 2}).snapshot().run()
    refused(lab, "FINDING_DISAGREES")
    lab = Lab(tmp_path / "junk").position().finding().snapshot().run()
    c = sqlite3.connect(str(lab.path))
    c.execute("DROP TRIGGER execution_reconciliations_no_update")                 # a damaged finding (only a hand edit could do this)
    c.execute("UPDATE execution_reconciliations SET detail_json = 'not json'")
    c.commit()
    c.close()
    refused(lab, "FINDING_DISAGREES")


def test_C11_a_live_executor_run_within_600_s_is_required_and_error_null_or_empty_both_count(tmp_path):
    refused(Lab(tmp_path / "none").position().finding().snapshot(), "EXECUTOR_NOT_RUNNING")
    refused(Lab(tmp_path / "old").position().finding().snapshot().run(started=-601), "EXECUTOR_NOT_RUNNING")
    refused(Lab(tmp_path / "err").position().finding().snapshot().run(started=-601).run(started=-10, result="ERROR", error="BrokerError: x"), "EXECUTOR_NOT_RUNNING")
    refused(Lab(tmp_path / "rerr").position().finding().snapshot().run(started=-10, error="boom"), "EXECUTOR_NOT_RUNNING")
    assert Lab(tmp_path / "null").position().finding().snapshot().run(started=-600).preview()["ok"] is True
    assert Lab(tmp_path / "empty").position().finding().snapshot().run(started=-30, error="").preview()["ok"] is True
    lab = Lab(tmp_path / "obs").position().finding().snapshot()
    ForwardValidationStore(lab.path).ensure()
    lab.sql("INSERT INTO forward_validation_samples (sample_id, at, trading_date, in_session, owner, mode) VALUES ('s1', ?, '2026-10-21', 1, 'x', 'MONITOR_ONLY')", (lab.at(-5),))
    refused(lab, "EXECUTOR_NOT_RUNNING")                                          # the observer's samples are not an executor run


def test_C12_a_partial_needs_an_exit_cycle_left(tmp_path):
    max_cycles = int(config.EXECUTION_MAX_EXIT_CYCLES)
    lab = Lab(tmp_path / "used").partial_case(exit_cycle=max_cycles, exit_reason="HARD_INVALIDATION")
    pv = refused(lab, "EXIT_ATTEMPTS_USED")
    assert pv["refusals"] == ["EXIT_ATTEMPTS_USED"] and "close the rest on Alpaca's website, then run apply for a close" in check(pv, 12)["detail"]
    assert Lab(tmp_path / "left").partial_case(exit_cycle=max_cycles - 1, exit_reason="HARD_INVALIDATION").preview()["ok"] is True
    assert Lab(tmp_path / "close").close_case(exit_cycle=max_cycles).preview()["ok"] is True      # a close needs no exit cycle


def test_C13_a_wrong_hash_refuses_and_a_refusal_never_prints_a_hash(tmp_path):
    lab = Lab(tmp_path).close_case()
    before = content(lab.path)
    current = lab.preview()["evidence_hash"]
    out = lab.apply(h="0" * 64)
    assert out["ok"] is False and out["refusals"] == ["EVIDENCE_HASH_MISMATCH"] and content(lab.path) == before
    assert current not in json.dumps(out) and not [k for k in out if "hash" in k]   # design section 5: the hash comes from preview only; a retry needs a new preview
    lab2 = Lab(tmp_path / "pre").close_case().mode("PAPER_AUTO")                    # the same for a precondition refusal with the right hash
    current2 = lab2.preview()["evidence_hash"]
    out2 = lab2.apply(h=current2)
    assert out2["refusals"] == ["PAPER_AUTO_ON"] and current2 not in json.dumps(out2) and not [k for k in out2 if "hash" in k]


def test_C14_missing_or_empty_arguments_refuse(tmp_path, capsys):
    lab = Lab(tmp_path).close_case()
    before = content(lab.path)
    for argv in (["apply", "--position-id", PID, "--evidence-hash", "h", "--approved-by", "me"], ["apply", "--position-id", PID], ["preview"], []):
        with pytest.raises(SystemExit) as e:
            R.main(argv, path=lab.path, now=lab.now)
        assert e.value.code == 2, argv
    for who, why in (("", "x"), ("me", "   "), ("x" * 501, "y"), ("me", "sold\nintervention: forged line"), ("me\t", "r\x7f")):
        out = lab.apply(who=who, why=why)
        assert out["ok"] is False and out["refusals"] == ["ARGUMENTS"] and out["written"] is False and out["usage_error"] is True, (who, why)
    h = lab.preview()["evidence_hash"]
    for who, why in (("", "r"), ("me", "two\nlines")):                           # a usage error exits 2, as argparse's own
        assert R.main(["apply", "--position-id", PID, "--evidence-hash", h, "--approved-by", who, "--reason", why, "--i-confirmed-flat-on-the-website"],
                      path=lab.path, now=lab.now) == 2, (who, why)
    assert content(lab.path) == before


def test_C15_a_database_path_that_does_not_exist_refuses_and_creates_nothing(tmp_path, capsys):
    missing = tmp_path / "nowhere" / "stock_agent.db"
    assert R.main(["preview", "--position-id", PID], path=missing) == 2
    assert R.main(["apply", "--position-id", PID, "--evidence-hash", "h", "--approved-by", "me", "--reason", "r", "--i-confirmed-flat-on-the-website"], path=missing) == 2
    assert not missing.exists() and not missing.parent.exists()
    assert "database file missing" in capsys.readouterr().out


def test_C16_a_strict_read_a_renamed_column_refuses_with_a_read_error(tmp_path, capsys):
    for table, col in (("paper_positions", "attention_reason"), ("account_refresh_liveness", "consecutive_failures"), ("execution_runs", "error"),
                       ("execution_reconciliations", "detail_json")):
        lab = Lab(tmp_path / table).close_case()
        h = lab.preview()["evidence_hash"]
        lab.sql(f"ALTER TABLE {table} RENAME COLUMN {col} TO {col}_gone")
        before = content(lab.path)
        assert R.main(["preview", "--position-id", PID], path=lab.path, now=lab.now) == 2, table
        assert R.main(["apply", "--position-id", PID, "--evidence-hash", h, "--approved-by", "me", "--reason", "r", "--i-confirmed-flat-on-the-website"],
                      path=lab.path, now=lab.now) == 2, table
        assert content(lab.path) == before and "read error" in capsys.readouterr().out
    lab = Lab(tmp_path / "manual").close_case()
    lab.manual(state="FILLED")
    lab.sql("ALTER TABLE alpaca_paper_order_intents RENAME COLUMN state TO state_gone")
    assert R.main(["preview", "--position-id", PID], path=lab.path, now=lab.now) == 2           # only a MISSING 4.6B table reads as none


def test_C17_apply_twice_the_second_is_refused_and_one_set_of_rows_exists(tmp_path):
    lab = Lab(tmp_path).close_case()
    h = lab.preview()["evidence_hash"]
    assert lab.apply(h=h)["ok"] is True
    again = lab.apply(h=h)
    assert again["ok"] is False and "NOT_RECOVERABLE" in again["refusals"] and "EVIDENCE_HASH_MISMATCH" in again["refusals"]
    assert [e["kind"] for e in lab.events()] == ["MANUAL_RECOVERY", "CLOSED"]
    lab2 = Lab(tmp_path / "p").partial_case()
    h = lab2.preview()["evidence_hash"]
    assert lab2.apply(h=h)["ok"] is True
    assert lab2.apply(h=h)["refusals"][0] == "EVIDENCE_HASH_MISMATCH"             # the quantity moved: the old hash no longer matches
    assert [e["kind"] for e in lab2.events()] == ["MANUAL_RECOVERY"]


def test_C18_the_hash_covers_both_quantities_the_reason_and_updated_at_never_the_snapshot_time(tmp_path):
    lab = Lab(tmp_path).close_case()
    h = lab.preview()["evidence_hash"]
    assert h == R.evidence_hash(PID, 5, 0, "BROKER_FLAT_INTERNAL_OPEN", lab.at(-300))
    lab.snapshot(refreshed=-10)                                                    # two more refreshes: the hash does not move
    lab.snapshot(refreshed=-5)
    assert lab.preview()["evidence_hash"] == h and lab.preview()["snapshot"]["refreshed_at"] == lab.at(-5)
    variants = {R.evidence_hash(PID, 4, 0, "BROKER_FLAT_INTERNAL_OPEN", lab.at(-300)), R.evidence_hash(PID, 5, 1, "BROKER_FLAT_INTERNAL_OPEN", lab.at(-300)),
                R.evidence_hash(PID, 5, 0, "QTY_MISMATCH", lab.at(-300)), R.evidence_hash(PID, 5, 0, "BROKER_FLAT_INTERNAL_OPEN", lab.at(-299)),
                R.evidence_hash("pos-2", 5, 0, "BROKER_FLAT_INTERNAL_OPEN", lab.at(-300))}
    assert h not in variants and len(variants) == 5


def test_C19_a_close_without_the_website_confirmation_is_refused_and_the_flag_is_an_attestation(tmp_path):
    lab = Lab(tmp_path).close_case()
    before = content(lab.path)
    out = lab.apply(confirmed=False)
    assert out["ok"] is False and out["refusals"] == ["CONFIRMATION_REQUIRED"] and content(lab.path) == before
    assert any("attestation, not evidence" in e for e in out["errors"])
    lab2 = Lab(tmp_path / "p").partial_case()
    assert lab2.apply(confirmed=False)["ok"] is True                               # a partial needs no flat confirmation


# ================================================================ A apply: the rows and the sentences ====================================
def test_A01_a_close_writes_exactly_the_design_rows_and_keeps_exit_qty_and_realized_pnl(tmp_path):
    lab = Lab(tmp_path).close_case(qty=3, exit_qty=2, realized_pnl="12.34", exit_reason="HARD_INVALIDATION", entry_qty=5)
    lab.finding(detail={"internal_qty": 3}, at=-280)
    h = lab.preview()["evidence_hash"]
    others = {t: lab.rows(f"SELECT * FROM {t}") for t in ("execution_intents", "execution_reconciliations", "execution_runs", "execution_mode", "account_refresh_snapshot",
                                                           "account_refresh_attempts", "account_refresh_liveness", "execution_controls")}
    out = lab.apply(h=h)
    assert out["ok"] is True and out["case"] == "CLOSE" and out["events"] == ["MANUAL_RECOVERY", "CLOSED"]
    p = lab.pos()
    assert (p["state"], p["quantity"], p["exit_reason"], p["attention_reason"], p["closed_at"], p["updated_at"]) == ("CLOSED", 0, "MANUAL_EXTERNAL", None, iso(NOW), iso(NOW))
    assert (p["exit_qty"], p["realized_pnl"], p["entry_qty"], p["average_entry_price"]) == (2, "12.34", 5, "100.00")
    ev = lab.events()
    assert [(e["kind"], e["from_state"], e["to_state"]) for e in ev] == [("MANUAL_RECOVERY", "ATTENTION", "CLOSED"), ("CLOSED", "ATTENTION", "CLOSED")]
    d = ev[0]["detail"]
    assert (d["actor"], d["reason"], d["evidence_hash"], d["internal_qty"], d["broker_qty"], d["original_exit_reason"], d["case"]) == (
        "operator", "sold on Alpaca's website", h, 3, 0, "HARD_INVALIDATION", "CLOSE")
    assert ev[1]["reason"] == "MANUAL_EXTERNAL" and "pnl_snapshot" not in ev[1]["detail"] and "realized_pnl" not in ev[1]["detail"]   # no P/L claim
    assert {t: lab.rows(f"SELECT * FROM {t}") for t in others} == others


def test_A02_a_partial_sets_the_broker_quantity_and_qty_mismatch_and_keeps_the_state(tmp_path):
    lab = Lab(tmp_path).partial_case(exit_reason="HARD_INVALIDATION")
    out = lab.apply()
    assert out["ok"] is True and out["case"] == "PARTIAL" and out["events"] == ["MANUAL_RECOVERY"]
    p = lab.pos()
    assert (p["state"], p["quantity"], p["attention_reason"], p["exit_reason"], p["closed_at"]) == ("ATTENTION", 2, "QTY_MISMATCH", "HARD_INVALIDATION", None)
    ev = lab.events()
    assert [(e["kind"], e["from_state"], e["to_state"]) for e in ev] == [("MANUAL_RECOVERY", "ATTENTION", "ATTENTION")]
    assert (ev[0]["detail"]["internal_qty"], ev[0]["detail"]["broker_qty"], ev[0]["detail"]["original_exit_reason"]) == (5, 2, "HARD_INVALIDATION")
    for reason, cycle in (("BROKER_FLAT_INTERNAL_OPEN", 0), ("EXIT_ATTEMPTS_EXHAUSTED", 1)):          # the reason is SET to QTY_MISMATCH: that is what lets the row clear
        other = Lab(tmp_path / reason).position(reason=reason, exit_cycle=cycle, updated=-300).finding(kind="QTY_MISMATCH", detail={"internal_qty": 5, "broker_qty": 2}, at=-290)
        other.snapshot(positions=[{"symbol": "SNDK", "qty": "2", "side": "long"}]).run()
        assert other.apply()["ok"] is True and other.pos()["attention_reason"] == "QTY_MISMATCH" and other.pos()["state"] == "ATTENTION", reason


def test_A03_preview_prints_s1_with_the_symbol_and_age_and_the_coming_sell_on_every_partial(tmp_path):
    pv = Lab(tmp_path / "c").close_case().preview()
    assert pv["notes"][0] == "Do not trade SNDK on Alpaca's website between this preview and the first cycle after apply. The account snapshot is 30 s old."
    assert R.SELL_TEXT not in pv["notes"] and pv["will_change"]["state"] == "ATTENTION -> CLOSED" and pv["writes"] == "none"
    assert pv["database"] == str(Lab(tmp_path / "c").path) and pv["settings"] == {"ACCOUNT_REFRESH_SECONDS": 60.0, "ACCOUNT_REFRESH_IDLE_SECONDS": 300.0,
                                                                                 "EXECUTION_MAX_EXIT_CYCLES": 3, "partial_max_age_s": 600, "executor_max_age_s": 600}
    for exit_reason in (None, "HARD_INVALIDATION"):
        pv = Lab(tmp_path / f"p{exit_reason}").partial_case(exit_reason=exit_reason).preview()
        assert pv["case"] == "PARTIAL" and "A SELL of the remainder can follow in the next cycle if any exit rule is met." in pv["notes"], exit_reason
        assert "Monitor only and Emergency Stop do not prevent that SELL; outside regular hours it can come at the first cycle of the next session, whoever is watching." in pv["notes"]
    pv = Lab(tmp_path / "r").partial_case(exit_cycle=3).preview()                  # refused: the sentence still prints for a partial
    assert pv["ok"] is False and R.SELL_TEXT in pv["notes"] and pv["will_change"] is None


def test_A04_apply_prints_the_website_check_after_a_close(tmp_path):
    arm = ("Paper Auto stays off until you enable it, or until the scheduled arm does if PAPER_AUTO_RUN_POLICY=SCHEDULED_ARM and PAPER_AUTO_SCHEDULED_ARM_ENABLED=true "
           "are both set (both are off by default).")
    out = Lab(tmp_path).close_case().apply()
    assert CHECK_TEXT in out["notes"] and any("attestation, not evidence" in n for n in out["notes"]) and arm in out["notes"] and out["database"].endswith("recovery.db")
    assert "Watch the first cycle after apply, without restarting the server before it, and read the Risk card." in out["notes"]
    out = Lab(tmp_path / "p").partial_case().apply()
    assert R.SELL_TEXT in out["notes"] and R.SELL_NOTE in out["notes"] and CHECK_TEXT not in out["notes"] and arm in out["notes"]


def test_A05_the_compare_and_set_refuses_a_change_inside_the_transaction_and_rolls_everything_back(tmp_path):
    lab = Lab(tmp_path).close_case()
    before = content(lab.path)
    out = lab.apply(_before_write=lambda conn: conn.execute("UPDATE paper_positions SET updated_at = ? WHERE position_id = ?", (lab.at(-1), PID)))
    assert out["ok"] is False and out["refusals"] == ["POSITION_CHANGED"] and content(lab.path) == before
    lab2 = Lab(tmp_path / "q").partial_case()
    out = lab2.apply(_before_write=lambda conn: conn.execute("UPDATE paper_positions SET quantity = 4 WHERE position_id = ?", (PID,)))
    assert out["refusals"] == ["POSITION_CHANGED"] and lab2.pos()["quantity"] == 5
    for name, change in (("state", "state = 'EXIT_REQUIRED'"), ("reason", "attention_reason = 'QTY_MISMATCH'")):   # the other two compared columns, each on its own
        lab3 = Lab(tmp_path / name).close_case()
        before3 = content(lab3.path)
        out = lab3.apply(_before_write=lambda conn, c=change: conn.execute(f"UPDATE paper_positions SET {c} WHERE position_id = ?", (PID,)))
        assert out["ok"] is False and out["refusals"] == ["POSITION_CHANGED"] and content(lab3.path) == before3, name


def test_A06_the_command_neither_reads_nor_writes_the_kill_never_writes_the_mode_and_never_reads_the_account(tmp_path):
    lab = Lab(tmp_path).close_case().mode("MONITOR_ONLY")
    c = sqlite3.connect(str(lab.path))
    c.execute("CREATE TABLE execution_safety_state (id INTEGER PRIMARY KEY, kill_active INTEGER)")
    c.execute("INSERT INTO execution_safety_state (id, kill_active) VALUES (1, 1)")
    c.commit()
    c.close()
    mode = lab.rows("SELECT * FROM execution_mode")
    assert lab.apply()["ok"] is True
    assert lab.rows("SELECT kill_active FROM execution_safety_state") == [{"kill_active": 1}] and lab.rows("SELECT * FROM execution_mode") == mode
    src = (ROOT / "execution" / "recovery.py").read_text(encoding="utf-8")
    for word in ("execution_safety_state", "kill_active", "UPDATE execution_mode", "INSERT INTO execution_mode", "account_json", "DELETE", "DROP", "REPLACE"):
        assert word not in src, word
    assert src.count("execution_mode") == 1 and "SELECT mode FROM execution_mode WHERE id = 1" in src   # the mode: read once (precondition 2), never written


def test_A07_the_cli_prints_json_and_exits_0_1_or_2(tmp_path, capsys):
    lab = Lab(tmp_path).close_case()
    assert R.main(["preview", "--position-id", PID], path=lab.path, now=lab.now) == 0
    pv = json.loads(capsys.readouterr().out)
    assert pv["ok"] is True and pv["evidence_hash"] and "equity" not in json.dumps(pv)
    assert R.main(["apply", "--position-id", PID, "--evidence-hash", pv["evidence_hash"], "--approved-by", "me", "--reason", "r"], path=lab.path, now=lab.now) == 1
    assert json.loads(capsys.readouterr().out)["refusals"] == ["CONFIRMATION_REQUIRED"]
    assert R.main(["apply", "--position-id", PID, "--evidence-hash", pv["evidence_hash"], "--approved-by", "me", "--reason", "r", "--i-confirmed-flat-on-the-website"],
                  path=lab.path, now=lab.now) == 0
    assert json.loads(capsys.readouterr().out)["written"] is True
    assert R.main(["preview", "--position-id", PID], path=lab.path, now=lab.now) == 1


# ================================================================ R races on a real file =================================================
def test_R01_an_executor_write_between_preview_and_apply_refuses_and_writes_nothing(tmp_path):
    lab = Lab(tmp_path).close_case()
    h = lab.preview()["evidence_hash"]
    ExecutionStore(lab.path).update_position(PID, NOW - timedelta(seconds=1), None, last_price="99.00")   # the executor's own write (updated_at moves)
    before = content(lab.path)
    out = lab.apply(h=h)
    assert out["ok"] is False and out["refusals"][0] == "EVIDENCE_HASH_MISMATCH" and content(lab.path) == before


def test_R02_two_concurrent_applies_write_exactly_one_set_of_rows(tmp_path):
    lab = Lab(tmp_path).close_case()
    h = lab.preview()["evidence_hash"]
    gate = threading.Barrier(2)
    results = []

    def go(who):
        gate.wait()
        results.append(R.apply(lab.path, PID, h, who, "race", True, NOW))
    ts = [threading.Thread(target=go, args=(w,)) for w in ("a", "b")]
    for t in ts:
        t.start()
    for t in ts:
        t.join(30)
    assert sorted(r["ok"] for r in results) == [False, True]
    assert [e["kind"] for e in lab.events()] == ["MANUAL_RECOVERY", "CLOSED"] and lab.pos()["state"] == "CLOSED"


def test_R03_apply_waits_for_a_writer_and_re_derives_from_its_commit(tmp_path):
    lab = Lab(tmp_path).close_case()
    h = lab.preview()["evidence_hash"]
    w = sqlite3.connect(str(lab.path), timeout=10, isolation_level=None)
    w.execute("BEGIN IMMEDIATE")                                                   # an executor transaction in flight
    w.execute("UPDATE paper_positions SET state = 'EXIT_REQUIRED', attention_reason = NULL, updated_at = ? WHERE position_id = ?", (lab.at(-1), PID))
    out = []
    t = threading.Thread(target=lambda: out.append(R.apply(lab.path, PID, h, "me", "r", True, NOW)))
    t.start()
    t.join(0.5)
    assert t.is_alive()                                                            # blocked on the writer's lock, not reading stale rows
    w.execute("COMMIT")
    w.close()
    t.join(30)
    assert out[0]["ok"] is False and "NOT_RECOVERABLE" in out[0]["refusals"] and lab.events() == []


# ================================================================ E end to end: the shared rig ===========================================
def rig(tmp_path, monkeypatch, name="rec.db"):
    from test_cash_flow_602 import started
    from test_forward_paper_610a import rig as rig610
    r = rig610(tmp_path, monkeypatch, name=name)
    started(r)
    return r


def cyc(r, advance=30):
    r.clock.advance(advance)
    return r.x.run_cycle(r.clock.now, "TEST")


def refresh(r, advance=5):
    r.clock.advance(advance)
    out = r.ref.refresh(trigger="TEST")
    assert out["result"] == "SUCCESS", out
    return out


def restart(r):
    """A process restart: a NEW executor on the same database and broker (nothing in memory survives), as tests/test_execution_60.py's reopen()."""
    r.x = ExecutionService(stream_fn=lambda: r.holder["s"], store=ExecutionStore(r.db), broker=r.mk(), clock=r.clock)
    r.holder["s"].execution = r.x
    return r.x


def enter(r, sym="SNDK"):
    from test_forward_paper_610a import armed
    pv = armed(r, sym)
    cyc(r)
    cyc(r)
    pos = r.x.store.open_position(sym)
    assert pos is not None and pos.quantity == pv.final_qty and pos.quantity >= 4, pos
    r.x.disable_auto(actor="test", now=r.clock.now)
    return pv, pos


def website(r, sym, qty):
    """The operator's trade on Alpaca's website: the broker now holds `qty` (the paper account only; nothing in the app sent it)."""
    r.sim.set_position(sym, qty)


def setup_ready(r, sym):
    """WATCH -> READY through the evaluator as the market-hours loop runs it, and the proposal WHATEVER its status, with its blocker codes
    (test_forward_paper_610a.go_ready asserts REVIEWABLE, which a stop or a halted day rightly prevents)."""
    from test_setups_58 import READY_IN, WATCH_IN, fx
    r.setups.evaluate_symbol(sym, trigger="SCHEDULED", inputs=fx(sym, **WATCH_IN))
    r.clock.advance(60)
    r.setups.evaluate_symbol(sym, trigger="SCHEDULED", inputs=fx(sym, **READY_IN))
    pv = r.s.proposals.current(sym, r.clock.now)
    return pv, [b["code"] if isinstance(b, dict) else b.code for b in pv.blockers]


def recover(r, pid, confirmed=True, who="operator", why="closed on Alpaca's website"):
    pv = R.preview(r.db, pid, r.clock.now)
    assert pv["ok"] is True, (pv["refusals"], pv["preconditions"])
    return pv, R.apply(r.db, pid, pv["evidence_hash"], who, why, confirmed, r.clock.now)


def no_broker_write(r, fn):
    posts, reqs = len(r.sim.posts), [m for m, _, _ in r.sim.requests]
    out = fn()
    assert len(r.sim.posts) == posts and [m for m, _, _ in r.sim.requests] == reqs      # the command makes no request at all
    return out


def test_E01_a_full_close_on_the_website_then_recovery_reconciles_healthy_and_paper_auto_needs_the_operator(tmp_path, monkeypatch):
    r = rig(tmp_path, monkeypatch)
    pv, pos = enter(r)
    website(r, "SNDK", 0)
    cyc(r)
    p = r.x.store.position(pos.position_id)
    assert (p.state, p.attention_reason, r.x.recon_healthy) == ("ATTENTION", "BROKER_FLAT_INTERNAL_OPEN", False)
    with pytest.raises(ExecutionRefused) as e:
        r.x.enable_auto(actor="test", now=r.clock.now)
    assert e.value.code == "RECONCILIATION_REQUIRED"
    r.s.safety.emergency_stop(reason="test", now=r.clock.now)
    r.s.safety.clear_emergency_stop(reason="test", now=r.clock.now)                # a Clear does not lift the reconciliation block
    with pytest.raises(ExecutionRefused):
        r.x.enable_auto(actor="test", now=r.clock.now)
    refresh(r)
    out = no_broker_write(r, lambda: recover(r, pos.position_id))[1]
    assert out["ok"] is True and out["case"] == "CLOSE"
    assert r.x.store.positions(open_only=True) == [] and r.x.mode()["mode"] == "MONITOR_ONLY"
    c = cyc(r)
    assert c["result"] == "RUN" and r.x.recon_healthy is True                        # nothing open, but the unhealthy executor reconciled
    assert cyc(r)["result"] == "IDLE"                                               # healthy again: back to idle
    r.x.enable_auto(actor="test", now=r.clock.now)
    assert r.x.mode()["mode"] == "PAPER_AUTO"


def test_E02_a_partial_close_then_recovery_clears_and_the_remainders_stop_fires(tmp_path, monkeypatch):
    from test_execution_60 import below_invalidation
    r = rig(tmp_path, monkeypatch)
    pv, pos = enter(r)
    keep = pos.quantity - 2
    website(r, "SNDK", keep)
    cyc(r)
    p = r.x.store.position(pos.position_id)
    assert (p.state, p.attention_reason) == ("ATTENTION", "QTY_MISMATCH")
    refresh(r)
    preview, out = recover(r, pos.position_id, confirmed=False)
    assert preview["case"] == "PARTIAL" and R.SELL_TEXT in preview["notes"] and out["ok"] is True
    assert r.x.store.position(pos.position_id).quantity == keep
    posts = len(r.sim.posts)
    below_invalidation(SimpleNamespace(x=r.x, sim=r.sim, clock=r.clock), pv)        # a stop breached while the row sat in ATTENTION
    cyc(r)
    kinds = [e["kind"] for e in r.x.store.position_events(pos.position_id)]
    assert "ATTENTION_CLEARED" in kinds and kinds.index("ATTENTION_CLEARED") > kinds.index("MANUAL_RECOVERY")
    exits = r.x.store.intents(intent_type="EXIT")
    assert len(r.sim.posts) == posts + 1 and len(exits) == 1 and exits[0].qty == keep                 # ONE SELL of the remainder, in the clearing cycle


def exhausted(r):
    from test_execution_60 import below_invalidation
    pv, pos = enter(r)
    below_invalidation(SimpleNamespace(x=r.x, sim=r.sim, clock=r.clock), pv)
    r.sim.post_modes = ["reject", "reject", "reject"]
    for _ in range(4):
        cyc(r)
    p = r.x.store.position(pos.position_id)
    assert (p.state, p.attention_reason, p.exit_cycle) == ("ATTENTION", "EXIT_ATTEMPTS_EXHAUSTED", 3)
    return pv, pos


def test_E03_exit_attempts_exhausted_then_a_website_close_recovers_even_after_two_more_refreshes(tmp_path, monkeypatch):
    r = rig(tmp_path, monkeypatch)
    pv, pos = exhausted(r)
    website(r, "SNDK", 0)
    cyc(r)
    refresh(r)
    h = R.preview(r.db, pos.position_id, r.clock.now)["evidence_hash"]
    refresh(r, 60)
    refresh(r, 60)                                                                 # the operator checks the website: the preview does not expire
    out = R.apply(r.db, pos.position_id, h, "operator", "sold the rest on the website", True, r.clock.now)
    assert out["ok"] is True and r.x.store.position(pos.position_id).state == "CLOSED"


def test_E04_exit_attempts_exhausted_then_a_partial_is_refused_until_the_rest_is_closed(tmp_path, monkeypatch):
    r = rig(tmp_path, monkeypatch)
    pv, pos = exhausted(r)
    website(r, "SNDK", 2)
    cyc(r)
    refresh(r)
    pv1 = R.preview(r.db, pos.position_id, r.clock.now)
    assert pv1["refusals"] == ["EXIT_ATTEMPTS_USED"] and pv1["case"] == "PARTIAL"
    website(r, "SNDK", 0)
    cyc(r)
    refresh(r)
    assert recover(r, pos.position_id)[1]["ok"] is True


def test_E05_flat_a_partial_buy_back_then_flat_again_refuses_until_a_restart_and_a_full_buy_back_is_allowed(tmp_path, monkeypatch):
    r = rig(tmp_path, monkeypatch)
    pv, pos = enter(r)
    website(r, "SNDK", 0)
    cyc(r)
    website(r, "SNDK", 2)                                                         # a partial buy-back: a QTY_MISMATCH finding (broker 2)
    cyc(r)
    website(r, "SNDK", 0)                                                         # flat again: that signature was written by this process already
    cyc(r)
    refresh(r)
    pv1 = R.preview(r.db, pos.position_id, r.clock.now)
    assert pv1["refusals"] == ["FINDING_DISAGREES"] and "Wait for the next cycle. If it still refuses, restart the server." in check(pv1, 10)["detail"]
    restart(r)                                                                     # the restart the text asks for: a new process writes the finding again
    cyc(r)
    refresh(r)
    assert recover(r, pos.position_id)[1]["ok"] is True
    r2 = rig(tmp_path / "full", monkeypatch, name="rec2.db")
    pv2, pos2 = enter(r2)
    website(r2, "SNDK", 0)
    cyc(r2)
    website(r2, "SNDK", pos2.quantity)                                            # a full buy-back clears the row (the quantities agree)
    cyc(r2)
    assert r2.x.store.position(pos2.position_id).state != "ATTENTION"
    website(r2, "SNDK", 0)
    cyc(r2)
    refresh(r2)
    assert recover(r2, pos2.position_id)[1]["ok"] is True


def test_E06_a_buy_back_after_the_flat_snapshot_is_blocked_by_internal_flat_broker_position(tmp_path, monkeypatch):
    r = rig(tmp_path, monkeypatch)
    pv, pos = enter(r)
    website(r, "SNDK", 0)
    cyc(r)
    refresh(r)
    h = R.preview(r.db, pos.position_id, r.clock.now)["evidence_hash"]
    website(r, "SNDK", pos.quantity)                                              # against S1: bought back after the snapshot's read
    assert R.apply(r.db, pos.position_id, h, "operator", "r", True, r.clock.now)["ok"] is True   # the command cannot see it (database rows only)
    cyc(r)
    assert r.x.recon_healthy is False and "broker position without an internal position" in "; ".join(r.x.recon_reasons)
    f = [x for x in r.x.store.findings(10) if x["kind"] == "INTERNAL_FLAT_BROKER_POSITION"]
    assert f and f[0]["blocking"] is True
    with pytest.raises(ExecutionRefused):
        r.x.enable_auto(actor="test", now=r.clock.now)


def test_E07_emergency_stop_active_throughout_recovery_works_and_the_stop_stays_on_and_refuses_every_entry(tmp_path, monkeypatch):
    r = rig(tmp_path, monkeypatch)
    pv, pos = enter(r)
    r.s.safety.emergency_stop(reason="test", now=r.clock.now)
    website(r, "SNDK", 0)
    cyc(r)
    refresh(r)
    before = r.s.safety.store.state()
    assert before.kill_active is True
    assert recover(r, pos.position_id)[1]["ok"] is True                           # correcting a record is not an entry: it works with the stop on
    assert r.s.safety.store.state() == before                                     # the command left the stop's row exactly as it was
    assert ExecutionStore(r.db).stop_state() == "ON" and r.s.safety.current(now=r.clock.now).kill_switch.active is True
    cyc(r)
    assert r.x.recon_healthy is True and r.x.mode()["mode"] == "MONITOR_ONLY"      # healthy again, and nothing resumed
    r.x.store.set_mode("PAPER_AUTO", r.clock.now, "test", None, {})              # even with the mode row at Paper Auto afterwards
    pv2, codes = setup_ready(r, "NVDA")
    assert pv2.effective_status == "BLOCKED" and codes == ["KILL_SWITCH_ACTIVE"]      # a ready setup, and the stop is the one thing in its way
    posts = len(r.sim.posts)
    cyc(r)
    cyc(r)
    assert len(r.sim.posts) == posts and r.x.store.open_position("NVDA") is None   # the stop refuses every new entry
    assert ExecutionStore(r.db).stop_state() == "ON"


def test_E08_paper_auto_on_refuses_writes_nothing_and_entries_do_not_resume(tmp_path, monkeypatch):
    from test_forward_paper_610a import go_ready
    r = rig(tmp_path, monkeypatch)
    pv, pos = enter(r)
    r.x.store.set_mode("PAPER_AUTO", r.clock.now, "test", None, {})              # Paper Auto on (the mode row, as the gate writes it)
    website(r, "SNDK", 0)
    cyc(r)
    refresh(r)
    before = content(r.db)
    pv1 = R.preview(r.db, pos.position_id, r.clock.now)
    assert pv1["refusals"] == ["PAPER_AUTO_ON"]
    assert R.apply(r.db, pos.position_id, pv1["evidence_hash"], "me", "r", True, r.clock.now)["refusals"] == ["PAPER_AUTO_ON"]
    assert content(r.db) == before
    go_ready(r, "NVDA")
    posts = len(r.sim.posts)
    cyc(r)
    assert len(r.sim.posts) == posts and not r.x.store.intents(symbol="NVDA")      # unhealthy: no new entry


def test_E09_unhealthy_from_broker_unavailable_with_nothing_open_clears_at_the_next_good_cycle(tmp_path, monkeypatch):
    r = rig(tmp_path, monkeypatch)
    assert cyc(r)["result"] == "IDLE"                                               # the executor's startup (nothing open: no reconcile yet)
    r.x.store.set_mode("PAPER_AUTO", r.clock.now, "test", None, {})
    r.sim.read_fail_paths = ["/v2/positions"]
    cyc(r)
    assert r.x.recon_healthy is False and "broker unavailable" in r.x.recon_reasons[0]
    r.x.disable_auto(actor="test", now=r.clock.now)                               # Monitor only, nothing open
    assert r.x.needs_broker() is False
    out = cyc(r)
    assert out["result"] == "RUN" and r.x.recon_healthy is True and out["summary"]["findings"] == 0
    assert cyc(r)["result"] == "IDLE"


def test_E10_enable_auto_reconciles_an_unhealthy_executor_with_nothing_open(tmp_path, monkeypatch):
    r = rig(tmp_path, monkeypatch)
    pv, pos = enter(r)
    website(r, "SNDK", 0)
    cyc(r)
    refresh(r)
    assert recover(r, pos.position_id)[1]["ok"] is True
    assert r.x.recon_healthy is False                                              # no cycle since: still the last reconcile's answer
    r.x.enable_auto(actor="test", now=r.clock.now)                                # enable_auto reconciles first and finds nothing wrong
    assert r.x.mode()["mode"] == "PAPER_AUTO" and r.x.recon_healthy is True


def test_E11_the_manual_external_surfaces_read_p_l_unverified_and_print_no_raw_number(tmp_path, monkeypatch):
    from forward_validation.pnl_view import decorate
    import api.routes.trader_view as TV
    r = rig(tmp_path, monkeypatch)
    monkeypatch.setitem(TV.STREAM, "instance", r.s)
    pv, pos = enter(r)
    website(r, "SNDK", 0)
    cyc(r)
    refresh(r)
    recover(r, pos.position_id)
    card = decorate(r.x.card("SNDK").model_dump())
    assert card["key"] == "CLOSED" and card["line"] == "Trade completed · — · Closed outside the app" and card["pnl_status"] == "UNVERIFIED"   # no earlier exit: the dash
    hist = decorate({"history": r.x.history(10)})["history"]
    row = next(h for h in hist if h.get("position_id") == pos.position_id)
    assert row["reason"] == "Closed outside the app" and row["pnl_status"] == "UNVERIFIED" and row["pnl_display"] is None
    evs = decorate({"events": r.x.store.position_events(None, 100)})["events"]
    closed = next(e for e in evs if e["kind"] == "CLOSED" and e["position_id"] == pos.position_id)
    assert closed["pnl_status"] == "UNVERIFIED" and closed["pnl_display"] is None
    v = r.fv.verification(r.x.store.position(pos.position_id))
    assert v["verified"] is False and {"PNL_NULL", "QTY_MISMATCH"} <= set(v["unverified_reasons"]), v


def test_E12_after_an_earlier_partial_bot_exit_the_card_line_reads_p_l_unverified_and_realized_pnl_is_kept(tmp_path, monkeypatch):
    from forward_validation.pnl_view import decorate
    from test_execution_60 import below_invalidation
    import api.routes.trader_view as TV
    r = rig(tmp_path, monkeypatch)
    monkeypatch.setitem(TV.STREAM, "instance", r.s)
    pv, pos = enter(r)
    below_invalidation(SimpleNamespace(x=r.x, sim=r.sim, clock=r.clock), pv)
    r.sim.auto_fill = "manual"
    cyc(r)                                                                         # the bot's exit is sent
    coid = r.x.store.intents(intent_type="EXIT")[0].client_order_id
    r.sim.fill(coid, 2, 95.0)                                                      # the bot exits 2 shares
    cyc(r)
    r.sim.set_status(coid, "canceled")
    website(r, "SNDK", 0)                                                          # the operator sells the rest on the website
    cyc(r)
    p = r.x.store.position(pos.position_id)
    assert (p.state, p.attention_reason, p.exit_qty) == ("ATTENTION", "BROKER_FLAT_INTERNAL_OPEN", 2) and p.realized_pnl is not None
    pnl = p.realized_pnl
    refresh(r)
    assert recover(r, pos.position_id)[1]["ok"] is True
    p = r.x.store.position(pos.position_id)
    assert (p.state, p.realized_pnl, p.exit_qty) == ("CLOSED", pnl, 2)
    card = decorate(r.x.card("SNDK").model_dump())
    assert card["line"] == "Trade completed · P/L unverified · Closed outside the app" and pnl not in card["line"]
    hist = decorate({"history": r.x.history(10)})["history"]
    row = next(h for h in hist if h.get("position_id") == pos.position_id)
    assert row["pnl_status"] == "UNVERIFIED" and row["pnl_display"] is None
    assert r.fv.verification(p)["verified"] is False


# ================================================================ V the intervention note and the evidence ================================
def fv_of(path, clock):
    ForwardValidationStore(path).ensure()
    xs = ExecutionStore(path)
    return ForwardValidationService(stream_fn=lambda: SimpleNamespace(execution=ExecutionService(lambda: None, xs, broker=W2.FB(), clock=clock)),
                                    store=ForwardValidationStore(path), clock=clock, owner="test-owner")


def session(lab, d, status="OPEN"):
    ForwardValidationStore(lab.path).ensure()
    lab.sql("INSERT INTO forward_validation_sessions (trading_date, session_status, created_at, updated_at) VALUES (?, ?, ?, ?)", (d, status, lab.at(-99999), lab.at(-99999)))


def recovered_at(lab, when, pid=PID, sym="SNDK", partial=False):
    """A recoverable position seeded relative to `when` and recovered at `when` (the real apply)."""
    lab.now = when
    (lab.partial_case(pid=pid, sym=sym) if partial else lab.position(pid=pid, sym=sym).finding(pid=pid, sym=sym).snapshot().run())
    out = lab.apply(pid=pid)
    assert out["ok"] is True, out
    return out


def notes(text):
    return text[text.index("NOTES\n"):]


def test_V01_the_reviews_scenario_a_partial_on_a_stored_current_summary_still_reaches_the_report(tmp_path):
    lab = Lab(tmp_path)
    t = datetime(2026, 10, 21, 18, 0, tzinfo=UTC)
    fv = fv_of(lab.path, lambda: t + timedelta(minutes=10))                          # the clock is later than the stored summary: a recomputed one would carry another computed_at
    s = fv.bracketed_summary("2026-10-21", t)
    assert s.get("pnl_watermark") is not None
    fv.store.put_daily("2026-10-21", t, "v", s)                                     # day D persisted with a current watermark
    recovered_at(lab, t + timedelta(minutes=5), partial=True)                        # a partial closes nothing: the watermark does not move
    served = fv.fresh_daily("2026-10-21")
    assert served["computed_at"] == s["computed_at"]                                 # the stored summary was served ...
    assert served["interventions"] == [{"code": "MANUAL_RECOVERY_IN_SESSION", "at": iso(t + timedelta(minutes=5)), "position_id": PID, "symbol": "SNDK", "actor": "operator"}]
    rep = REP.daily_report(served)                                                   # ... and the report prints the line
    assert f"  intervention: MANUAL_RECOVERY_IN_SESSION SNDK at {iso(t + timedelta(minutes=5))} (by operator)\n" in notes(rep)
    assert "interventions" not in json.loads(lab.rows("SELECT summary_json FROM forward_validation_daily_summary")[0]["summary_json"])


def test_V02_a_close_prints_exactly_once_whether_or_not_the_summary_was_rebuilt(tmp_path):
    lab = Lab(tmp_path)
    t = datetime(2026, 10, 21, 18, 0, tzinfo=UTC)
    fv = fv_of(lab.path, lambda: t + timedelta(minutes=10))
    fv.store.put_daily("2026-10-21", t, "v", fv.bracketed_summary("2026-10-21", t))
    recovered_at(lab, t + timedelta(minutes=5))
    fresh = fv.fresh_daily("2026-10-21")                                             # the close moved the watermark: computed fresh
    assert REP.daily_report(fresh).count("  intervention: MANUAL_RECOVERY_IN_SESSION SNDK at ") == 1
    fv.store.put_daily("2026-10-21", t, "v", fv.bracketed_summary("2026-10-21"))   # rebuilt and stored again
    stored = fv.fresh_daily("2026-10-21")
    assert REP.daily_report(stored).count("  intervention: MANUAL_RECOVERY_IN_SESSION SNDK at ") == 1


def test_V03_fresh_dailies_attaches_each_days_items_and_nothing_from_another_day(tmp_path):
    lab = Lab(tmp_path)
    days = ("2026-10-19", "2026-10-20", "2026-10-21")
    for d in days:
        session(lab, d)
    fv = fv_of(lab.path, lambda: datetime(2026, 10, 22, 12, 0, tzinfo=UTC))
    for d in days:
        fv.store.put_daily(d, NOW, "v", fv.bracketed_summary(d, NOW))
    recovered_at(lab, datetime(2026, 10, 20, 17, 0, tzinfo=UTC), pid="p20")
    recovered_at(lab, datetime(2026, 10, 21, 17, 0, tzinfo=UTC), pid="p21", sym="AMD")
    got = {s["trading_date"]: [i["position_id"] for i in s["interventions"]] for s in fv.fresh_dailies()}
    assert got == {"2026-10-19": [], "2026-10-20": ["p20"], "2026-10-21": ["p21"]}
    assert all(fv.fresh_daily(d)["interventions"] == next(s["interventions"] for s in fv.fresh_dailies() if s["trading_date"] == d) for d in days)


def test_V04_a_day_with_no_recovery_gives_a_byte_identical_report_and_unchanged_stored_fields(tmp_path):
    lab = Lab(tmp_path)
    fv = fv_of(lab.path, lambda: NOW)
    s = fv.fresh_daily("2026-10-21")
    assert s["interventions"] == []
    base = {k: v for k, v in s.items() if k != "interventions"}
    assert REP.daily_report(s) == REP.daily_report(base)                            # the absent key and the empty list print the same nothing
    assert set(base) == set(fv.bracketed_summary("2026-10-21", NOW)) and "interventions" not in fv.bracketed_summary("2026-10-21", NOW) and "interventions" not in fv.daily_summary("2026-10-21", NOW)
    assert fv._pnl_content(s) == fv._pnl_content(base) and s["attention"] == base["attention"]
    cols, rows = REP.export_rows(fv, "daily")
    assert "interventions" not in cols and all("interventions" not in r for r in rows)


def test_V05_an_absent_key_prints_nothing_and_none_prints_unreadable(tmp_path):
    lab = Lab(tmp_path)
    fv = fv_of(lab.path, lambda: NOW)
    s = fv.daily_summary("2026-10-21", NOW)
    assert "interventions" not in s and "intervention" not in notes(REP.daily_report(s))
    assert "  interventions: unreadable\n" in notes(REP.daily_report({**s, "interventions": None}))
    assert "intervention" not in notes(REP.daily_report({**s, "interventions": []}))


def test_V06_the_note_never_becomes_critical_no_attention_raised_no_early_stop_and_the_monitor_reads_warn(tmp_path):
    lab = Lab(tmp_path)
    recovered_at(lab, NOW - timedelta(minutes=5))
    lab.mode("PAPER_AUTO")                                                         # set after the recovery (it refuses in PAPER_AUTO)
    fv = fv_of(lab.path, lambda: NOW)
    att = fv.attention(NOW)
    note = [a for a in att if a["code"] == "MANUAL_RECOVERY_IN_SESSION"]
    assert len(note) == 1 and note[0]["critical"] is False and note[0]["count"] == 1
    assert "MANUAL_RECOVERY_IN_SESSION" not in CRITICAL_CODES and "MANUAL_RECOVERY_UNREADABLE" not in CRITICAL_CODES
    assert fv._early_stop(NOW, att, {"trading_date": "2026-10-21"}) is None
    assert fv.store.events(kind="ATTENTION_RAISED", limit=10) == [] and fv.store.events(kind="EARLY_STOP", limit=10) == []
    assert {c["control_id"]: c["last_result"] for c in fv.store.controls()}["attention_monitor"] == "WARN"
    assert lab.rows("SELECT mode FROM execution_mode") == [{"mode": "PAPER_AUTO"}]


def test_V07_a_read_error_is_never_shown_as_no_recovery(tmp_path):
    lab = Lab(tmp_path)
    recovered_at(lab, NOW - timedelta(minutes=5))
    fv = fv_of(lab.path, lambda: NOW)
    fv.store.put_daily("2026-10-21", NOW, "v", fv.bracketed_summary("2026-10-21", NOW))   # a stored day, so fresh_dailies() has a row to attach to
    lab.sql("ALTER TABLE position_events RENAME COLUMN detail_json TO detail_gone")
    assert fv.interventions("2026-10-21") is None
    att = {a["code"]: a for a in fv.attention(NOW)}
    assert att["MANUAL_RECOVERY_UNREADABLE"]["text"] == "manual recovery records could not be read" and att["MANUAL_RECOVERY_UNREADABLE"]["critical"] is False
    assert "MANUAL_RECOVERY_IN_SESSION" not in att
    assert "  interventions: unreadable\n" in notes(REP.daily_report(fv.fresh_daily("2026-10-21")))
    dailies = fv.fresh_dailies()
    assert [s["trading_date"] for s in dailies] == ["2026-10-21"] and dailies[0]["interventions"] is None
    fv2 = ForwardValidationService(stream_fn=lambda: SimpleNamespace(execution=None), store=ForwardValidationStore(tmp_path / "absent.db"), clock=lambda: NOW)
    assert fv2.interventions("2026-10-21") is None and not (tmp_path / "absent.db").exists()


def test_V08_the_new_york_date_boundary(tmp_path):
    lab = Lab(tmp_path)
    for d in ("2026-10-20", "2026-10-21", "2026-10-22"):
        session(lab, d)
    recovered_at(lab, datetime(2026, 10, 22, 3, 30, tzinfo=UTC), pid="late")      # 23:30 New York on the 21st
    recovered_at(lab, datetime(2026, 10, 22, 4, 30, tzinfo=UTC), pid="early")     # 00:30 New York on the 22nd
    fv = fv_of(lab.path, lambda: NOW)
    assert [i["position_id"] for i in fv.interventions("2026-10-21")] == ["late"]
    assert [i["position_id"] for i in fv.interventions("2026-10-22")] == ["early"]
    assert ny_window("2026-10-21")[1] == "2026-10-22T04:00:00+00:00"


def test_V09_a_weekend_recovery_reaches_mondays_report_and_a_monday_holiday_moves_it_to_tuesday(tmp_path):
    sat = datetime(2026, 10, 24, 15, 0, tzinfo=UTC)
    lab = Lab(tmp_path / "w")
    for d in ("2026-10-23", "2026-10-26", "2026-10-27"):                           # Friday, Monday, Tuesday have session rows; the weekend has none
        session(lab, d)
    recovered_at(lab, sat)
    fv = fv_of(lab.path, lambda: sat)
    assert [fv.interventions(d) != [] for d in ("2026-10-23", "2026-10-26", "2026-10-27")] == [False, True, False]
    for now, shown in ((sat, True), (datetime(2026, 10, 25, 15, 0, tzinfo=UTC), True), (datetime(2026, 10, 27, 3, 59, tzinfo=UTC), True),
                       (datetime(2026, 10, 27, 4, 0, tzinfo=UTC), False)):            # live from Saturday through the end of Monday (New York)
        assert ("MANUAL_RECOVERY_IN_SESSION" in {a["code"] for a in fv.attention(now)}) is shown, now
    lab2 = Lab(tmp_path / "h")
    for d in ("2026-10-23", "2026-10-27"):                                          # Monday a holiday: no session row
        session(lab2, d)
    recovered_at(lab2, sat)
    fv2 = fv_of(lab2.path, lambda: sat)
    assert fv2.interventions("2026-10-26") != [] and fv2.interventions("2026-10-27") != [] and fv2.interventions("2026-10-23") == []
    assert "  intervention: MANUAL_RECOVERY_IN_SESSION SNDK at 2026-10-24T15:00:00+00:00 (by operator)\n" in notes(REP.daily_report(fv2.fresh_daily("2026-10-27")))


def test_V10_the_live_code_leaves_after_the_next_sessions_day_while_the_dated_report_keeps_the_line(tmp_path):
    lab = Lab(tmp_path)
    for d in ("2026-10-21", "2026-10-22"):
        session(lab, d)
    recovered_at(lab, NOW)
    fv = fv_of(lab.path, lambda: NOW)
    assert "MANUAL_RECOVERY_IN_SESSION" in {a["code"] for a in fv.attention(NOW)}
    later = datetime(2026, 10, 22, 15, 0, tzinfo=UTC)
    assert "MANUAL_RECOVERY_IN_SESSION" not in {a["code"] for a in fv.attention(later)}
    assert "  intervention: MANUAL_RECOVERY_IN_SESSION SNDK at " in notes(REP.daily_report(fv.fresh_daily("2026-10-21")))


def test_V11_forward_capture_writes_no_exited_decision_or_outcome_for_a_recovered_close_or_a_remainder_closed_later(tmp_path, monkeypatch):
    from test_forward_paper_610a import armed, by, obs
    from test_execution_60 import below_invalidation
    r = rig(tmp_path, monkeypatch)
    obs(r)                                                                          # the capture epoch
    pv, pos = enter(r)
    website(r, "SNDK", 0)
    cyc(r)
    refresh(r)
    recover(r, pos.position_id)
    obs(r)
    assert pos.position_id not in {d["position_id"] for d in by(r, "EXITED")} and r.store.outcome(pos.position_id) is None
    r2 = rig(tmp_path / "rem", monkeypatch, name="rec2.db")
    obs(r2)
    pv2, pos2 = enter(r2)
    website(r2, "SNDK", pos2.quantity - 2)
    cyc(r2)
    refresh(r2)
    recover(r2, pos2.position_id)
    below_invalidation(SimpleNamespace(x=r2.x, sim=r2.sim, clock=r2.clock), pv2)
    cyc(r2)                                                                         # cleared, and the remainder's exit is sent
    cyc(r2)                                                                         # the bot closes the remainder with a normal reason
    p2 = r2.x.store.position(pos2.position_id)
    assert p2.state == "CLOSED" and p2.exit_reason == "HARD_INVALIDATION"
    obs(r2)
    assert pos2.position_id not in {d["position_id"] for d in by(r2, "EXITED")} and r2.store.outcome(pos2.position_id) is None
    v = r2.fv.verification(p2)
    assert v["verified"] is False and "QTY_MISMATCH" in v["unverified_reasons"]


def test_V12_recovered_positions_are_excluded_in_the_query_so_they_never_fill_the_limit_200(tmp_path, monkeypatch):
    from test_forward_paper_610a import by, obs
    from test_execution_60 import below_invalidation
    r = rig(tmp_path, monkeypatch)
    obs(r)                                                                          # the capture epoch
    epoch = r.store.state()["epoch"]
    r.x.store.ensure()
    c = sqlite3.connect(str(r.db))
    for i in range(201):                                                            # more recovered closes than one query's LIMIT 200, all closed first
        pid = f"rec-{i:03d}"
        c.execute("INSERT INTO paper_positions (position_id, execution_id, symbol, quantity, entry_qty, average_entry_price, opened_at, state, exit_reason, closed_at, updated_at) "
                  "VALUES (?, ?, 'ZZZ', 0, 1, '10.00', ?, 'CLOSED', 'MANUAL_EXTERNAL', ?, ?)", (pid, "x-" + pid, epoch, epoch, epoch))
        c.execute("INSERT INTO position_events (event_id, position_id, at, kind, from_state, to_state, reason, detail_json) VALUES (?, ?, ?, 'MANUAL_RECOVERY', 'ATTENTION', 'CLOSED', 'r', '{}')",
                  (uuid.uuid4().hex, pid, epoch))
    c.commit()
    c.close()
    pv, pos = enter(r)
    below_invalidation(SimpleNamespace(x=r.x, sim=r.sim, clock=r.clock), pv)
    cyc(r)                                                                          # the bot's exit is sent
    cyc(r)                                                                          # and filled: a normal close, after the 201 recovered ones
    assert r.x.store.position(pos.position_id).state == "CLOSED"
    obs(r)
    exited = {d["position_id"] for d in by(r, "EXITED")}
    assert pos.position_id in exited and not any(p.startswith("rec-") for p in exited)


# ================================================================ T the task's scenarios =================================================
def test_T01_a_matching_startup_needs_no_recovery_and_stays_idle_with_no_broker_call(tmp_path, monkeypatch):
    r = rig(tmp_path, monkeypatch)
    n = len(r.sim.requests)
    assert cyc(r)["result"] == "IDLE" and len(r.sim.requests) == n               # healthy with nothing open: idle, as before
    pv, pos = enter(r)
    cyc(r)
    p = r.x.store.position(pos.position_id)
    assert p.state in ("OPEN", "HEALTHY", "WEAKENING") and r.x.recon_healthy is True
    refresh(r)
    assert "NOT_RECOVERABLE" in R.preview(r.db, pos.position_id, r.clock.now)["refusals"]
    restart(r)                                                                     # a process restart while the bot's record and the broker agree
    posts = len(r.sim.posts)
    cyc(r)
    p = r.x.store.position(pos.position_id)
    assert p.state in ("OPEN", "HEALTHY", "WEAKENING") and p.quantity == pos.quantity and r.x.recon_healthy is True
    assert len(r.sim.posts) == posts and r.x.mode()["mode"] == "MONITOR_ONLY"      # the startup reconcile sends nothing and resumes nothing
    refresh(r)
    assert "NOT_RECOVERABLE" in R.preview(r.db, pos.position_id, r.clock.now)["refusals"]   # a matching state needs no recovery, and gets none


def test_T02_a_restart_with_a_bot_position_in_attention_recovers_and_stays_monitor_only(tmp_path, monkeypatch):
    r = rig(tmp_path, monkeypatch)
    pv, pos = enter(r)
    website(r, "SNDK", 0)
    cyc(r)
    restart(r)                                                                     # a process restart: a new executor on the same file
    cyc(r)
    assert r.x.store.position(pos.position_id).state == "ATTENTION" and r.x.mode()["mode"] == "MONITOR_ONLY"
    refresh(r)
    assert recover(r, pos.position_id)[1]["ok"] is True
    cyc(r)
    assert r.x.recon_healthy is True and r.x.mode()["mode"] == "MONITOR_ONLY"      # no automatic resumption


def test_T03_an_unmanaged_position_is_never_touched_or_adopted(tmp_path, monkeypatch):
    r = rig(tmp_path, monkeypatch)
    pv, pos = enter(r)
    website(r, "AMD", 7)                                                           # an external position the bot never owned
    website(r, "SNDK", 0)
    cyc(r)
    refresh(r)
    assert R.preview(r.db, "external-AMD", r.clock.now)["refusals"] == ["NOT_RECOVERABLE"]
    assert recover(r, pos.position_id)[1]["ok"] is True
    assert [p.symbol for p in r.x.store.positions(open_only=False)] == ["SNDK"] and r.sim.positions["AMD"]["qty"] == 7
    assert {f["kind"] for f in r.x.store.findings(50) if f["symbol"] == "AMD"} == {"EXTERNAL_POSITION"}


def test_T04_open_and_partially_filled_orders_refuse(tmp_path):
    lab = Lab(tmp_path / "x").close_case()
    lab.intent(state="PARTIALLY_FILLED")
    refused(lab, "INTENT_OPEN")
    lab = Lab(tmp_path / "o").position().finding().snapshot(orders=[{"id_short": "b", "symbol": "SNDK", "side": "sell", "status": "partially_filled"}]).run()
    refused(lab, "OPEN_ORDER_IN_SYMBOL")
    lab = Lab(tmp_path / "m").close_case()
    lab.manual(state="PARTIALLY_FILLED")
    refused(lab, "MANUAL_INTENT_ACTIVE")


def test_T05_a_broker_timeout_with_an_uncertain_outcome_refuses(tmp_path, monkeypatch):
    from test_execution_60 import below_invalidation
    r = rig(tmp_path, monkeypatch)
    pv, pos = enter(r)
    below_invalidation(SimpleNamespace(x=r.x, sim=r.sim, clock=r.clock), pv)
    r.sim.post_modes = ["timeout_after_record"]                                   # the exit POST times out after the broker recorded it
    r.sim.lookup_lag = 99
    cyc(r)
    it = r.x.store.intents(intent_type="EXIT")[0]
    assert it.state in X.INTENT_OPEN
    refresh(r)
    pv1 = R.preview(r.db, pos.position_id, r.clock.now)
    assert "INTENT_OPEN" in pv1["refusals"] and "NOT_RECOVERABLE" in pv1["refusals"]
    lab = Lab(tmp_path / "rr").close_case()
    lab.intent(state="RECONCILIATION_REQUIRED")
    refused(lab, "INTENT_OPEN")
    lab = Lab(tmp_path / "46b").close_case()
    lab.manual(state="SUBMISSION_PENDING")
    refused(lab, "MANUAL_INTENT_ACTIVE")


def test_T06_stale_or_unavailable_broker_data_refuses(tmp_path, monkeypatch):
    r = rig(tmp_path, monkeypatch)
    pv, pos = enter(r)
    website(r, "SNDK", 0)
    cyc(r)
    refresh(r)
    r.ref._read_attempts = 1
    r.sim.read_fail_paths = ["/v2/account"]
    r.clock.advance(5)
    assert r.ref.refresh(trigger="TEST")["result"] != "SUCCESS"                   # the refresh failed: the last good snapshot stays, with a failure
    assert R.preview(r.db, pos.position_id, r.clock.now)["refusals"] == ["SNAPSHOT_NOT_TRUSTED"]
    refresh(r)
    r.clock.advance(700)                                                           # the server stops: no refresh, no executor run
    assert set(R.preview(r.db, pos.position_id, r.clock.now)["refusals"]) == {"SNAPSHOT_TOO_OLD", "EXECUTOR_NOT_RUNNING"}


def test_T07_an_inconsistent_database_refuses(tmp_path):
    lab = Lab(tmp_path / "pos").close_case()
    lab.sql("ALTER TABLE paper_positions RENAME COLUMN exit_cycle TO exit_cycle_gone")
    with pytest.raises(R.RecoveryReadError):
        lab.preview()
    lab = Lab(tmp_path / "snap").close_case()
    lab.sql("UPDATE account_refresh_snapshot SET open_orders_json = '{}' WHERE id = 1")
    assert "SNAPSHOT_NOT_TRUSTED" in lab.preview()["refusals"]
    lab = Lab(tmp_path / "att").close_case()
    lab.sql("UPDATE account_refresh_snapshot SET refresh_id = 'no-such-attempt' WHERE id = 1")
    assert "SNAPSHOT_OLDER_THAN_FINDING" in lab.preview()["refusals"]
    lab = Lab(tmp_path / "type").close_case()
    lab.sql("UPDATE paper_positions SET quantity = 'five' WHERE position_id = ?", (PID,))   # a hand edit of the wrong type (the CHECK lets text through)
    h = R.evidence_hash(PID, 5, 0, "BROKER_FLAT_INTERNAL_OPEN", lab.at(-300))
    before = content(lab.path)
    assert R.main(["preview", "--position-id", PID], path=lab.path, now=lab.now) == 2
    assert R.main(["apply", "--position-id", PID, "--evidence-hash", h, "--approved-by", "me", "--reason", "r", "--i-confirmed-flat-on-the-website"],
                  path=lab.path, now=lab.now) == 2                                   # an unexpected error: printed, exit 2, never a traceback that reads as a refusal
    assert content(lab.path) == before


def test_T08_a_pause_stays_through_a_recovery_and_blocks_entries_even_with_paper_auto_on(tmp_path, monkeypatch):
    from test_forward_paper_610a import go_ready
    r = rig(tmp_path, monkeypatch)
    pv, pos = enter(r)
    r.s.safety.pause(reason="hold", now=r.clock.now)
    website(r, "SNDK", 0)
    cyc(r)
    refresh(r)
    before = r.s.safety.store.state()
    assert before.pause_active is True
    assert recover(r, pos.position_id)[1]["ok"] is True
    assert r.s.safety.store.state() == before                                     # the command left the hold's row exactly as it was
    cyc(r)
    v = r.s.safety.current(now=r.clock.now)
    assert v.no_new_entries.active is True and v.allowed_new_entry is False and r.x.mode()["mode"] == "MONITOR_ONLY"   # no automatic resumption
    r.x.enable_auto(actor="test", now=r.clock.now)                                # even after the operator arms Paper Auto: the pause decides
    go_ready(r, "NVDA")
    posts = len(r.sim.posts)
    cyc(r)
    cyc(r)
    assert len(r.sim.posts) == posts and r.x.store.open_position("NVDA") is None   # paused: no entry is sent
    assert r.s.safety.store.state().pause_active is True


def test_T09_a_latched_daily_loss_halt_stays_through_a_recovery_and_blocks_entries_even_with_paper_auto_on(tmp_path, monkeypatch):
    from test_cash_flow_602 import cycle, equity
    r = rig(tmp_path, monkeypatch)
    pv, pos = enter(r)
    equity(r, 94000)                                                              # a 6% loss on the day (the limit is 5%)
    v = cycle(r, 60)
    assert v.state == "HALTED" and "DAILY_LOSS_LIMIT_REACHED" in v.reason_codes and r.s.safety.store.state().daily_halt_active is True
    equity(r, 100000)                                                             # equity recovers; the day's halt does not
    assert cycle(r, 60).state == "HALTED"
    website(r, "SNDK", 0)
    cyc(r)
    refresh(r)
    before = r.s.safety.store.state()
    assert before.daily_halt_active is True
    assert recover(r, pos.position_id)[1]["ok"] is True                           # the record is corrected while the day is halted
    assert r.s.safety.store.state() == before                                     # the command left the latch exactly as it was (date, time, loss)
    cyc(r)
    assert r.x.recon_healthy is True and r.x.mode()["mode"] == "MONITOR_ONLY"      # healthy again, and nothing resumed
    r.x.enable_auto(actor="test", now=r.clock.now)                                # even after the operator arms Paper Auto: the halt decides
    pv2, codes = setup_ready(r, "NVDA")
    assert pv2.effective_status == "BLOCKED" and codes == ["DAILY_LOSS_HALT_ACTIVE"]  # a ready setup, and the halted day is the one thing in its way
    posts = len(r.sim.posts)
    cyc(r)
    cyc(r)
    v = r.s.safety.evaluate("TEST", r.clock.now)
    assert v.state == "HALTED" and "DAILY_LOSS_LIMIT_REACHED" in v.reason_codes and v.allowed_new_entry is False
    assert len(r.sim.posts) == posts and r.x.store.open_position("NVDA") is None   # the halted day: no entry is sent
    assert r.s.safety.store.state().daily_halt_active is True and r.s.safety.store.event_count("DAILY_HALT_TRIGGERED") == 1   # latched once, never re-armed


# ================================================================ S static pins ===========================================================
def test_S01_the_module_imports_no_network_client_broker_route_or_writer_and_runs_no_process():
    src = (ROOT / "execution" / "recovery.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    mods = {(n.module or "") for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)} | {a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
    names = {f"{n.module}.{a.name}" for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) for a in n.names}      # `from execution import broker` is caught too
    assert names == {"__future__.annotations", "datetime.datetime", "datetime.timezone", "decimal.Decimal", "decimal.InvalidOperation", "pathlib.Path", "typing.Any",
                     "typing.Callable", "typing.Dict", "typing.List", "typing.Optional", "typing.Tuple", "execution.INTENT_OPEN", "paper.alpaca_order_rules",
                     "setups.market_hours.is_regular_session"}, sorted(names)
    banned = ("requests", "urllib", "urllib3", "http", "httpx", "socket", "ssl", "aiohttp", "websocket", "websockets", "alpaca", "subprocess", "api", "trader_view", "safety",
              "execution.broker", "execution.simulated", "execution.service", "execution.scheduler", "execution.store", "account_refresh", "paper.alpaca_order_writer",
              "paper.alpaca_orders", "paper.alpaca_order_reads", "paper.alpaca_order_store", "paper.alpaca_readonly", "paper.alpaca_view", "forward_validation", "fit")
    assert not {m for m in mods if any(m == b or m.startswith(b + ".") for b in banned)}, mods
    assert mods <= {"__future__", "hashlib", "json", "sqlite3", "uuid", "datetime", "decimal", "pathlib", "typing", "config", "execution", "paper", "setups.market_hours",
                    "argparse", "sys"}, mods
    assert "def main(" in src and "/api/" not in src and "POST" not in src and "requests" not in src


def test_S02_no_http_route_or_button_was_added():
    from api.server import app
    assert not [p for p in app.openapi()["paths"] if "recover" in p.lower()]
    for rel in ("frontend/product_ui.js", "frontend/index.html", "frontend/paper_bot.js"):
        assert "execution/recovery" not in (ROOT / rel).read_text(encoding="utf-8") and "recover-position" not in (ROOT / rel).read_text(encoding="utf-8")


def test_S03_manual_external_has_its_text_and_is_not_a_strategy_exit_and_the_frozen_controls_are_unchanged():
    from forward_validation import frozen_controls, frozen_hash
    assert X.EXIT_TEXT["MANUAL_EXTERNAL"] == "Closed outside the app" and "MANUAL_EXTERNAL" not in X.EXIT_REASONS and R.EXIT_REASON == "MANUAL_EXTERNAL"
    assert frozen_controls()["exit_reasons"] == list(X.EXIT_REASONS) and X.VERSION == "execution_v5" and "Closed outside the app" not in json.dumps(frozen_controls())
    h = frozen_hash()
    saved = dict(X.EXIT_TEXT)
    try:
        del X.EXIT_TEXT["MANUAL_EXTERNAL"]                                              # the Stage 6.1 strategy contract does not read EXIT_TEXT: the hash does not move
        assert frozen_hash() == h
    finally:
        X.EXIT_TEXT.clear()
        X.EXIT_TEXT.update(saved)
