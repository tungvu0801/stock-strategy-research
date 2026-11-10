"""
Wave 2 PR A - exit-fill accounting, verification, fail-closed P/L and fresh read-only summaries (WAVE2_REMEDIATION_DESIGN.md sections 3.1 and
3.12; WAVE2_TEST_AND_ACCEPTANCE_PLAN.md section 3.1, T-A01 ... T-A45, and the offline checker tools/verify_exit_fills.py).

Simulation only: tmp_path SQLite; the real ExecutionService / ExecutionStore driven through _apply_order with hand-built broker order views (the
broker's CUMULATIVE filled_qty / filled_avg_price), the real stack on the simulated PAPER broker where a decision path matters, and the real
ForwardValidationService and API routes on the same file. No network, no credential, no real order.
"""
import ast
import json
import random
import re
import sqlite3
import subprocess
import threading
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from test_execution_60 import (arm, below_invalidation, cyc, entered, execution_config, make_stack, no_execution_wire)  # noqa: F401 - the rig and its two autouse fixtures
import wave2_pra_support as S
import freeze_ledger as FL  # Wave 2 PR B (D-1): T-A42 reads the 6.1 / 6.10A tests on PR A's own delta
from wave2_pra_support import DAY, Direct, T0, content_hash, order
from execution.engine import money
from execution.models import Intent
from execution.store import ExecutionStore
from forward_validation import VERSION as FV_VERSION
from forward_validation import metrics as M
from forward_validation import reports as REP
from forward_validation.service import ForwardValidationService
from forward_validation.store import ForwardValidationStore
from api.routes import trader_view as RT

ROOT = Path(__file__).resolve().parents[1]
BASE_W2 = "5d6f7dd1e1beb90fc5b2909a7dafabfd8e932a6a"
GOLDEN = json.loads((ROOT / "tests" / "wave2_pra_golden.json").read_text(encoding="utf-8"))
DATES = ["2026-10-15", "2026-10-16", "2026-10-19", "2026-10-20", DAY]          # five weekdays, DAY the latest


def at_base(rel: str) -> str:
    run = subprocess.run(["git", "show", f"{BASE_W2}:{rel}"], cwd=ROOT, capture_output=True)
    assert run.returncode == 0, f"{rel} is not in {BASE_W2} (fetch-depth 0 is required)"
    return run.stdout.decode("utf-8").replace("\r\n", "\n")


def reasons(d, p):
    return d.fv.verification(p)["unverified_reasons"]


def trade_of(d, p):
    return next(t for t in d.fv.closed_trades() if t["position_id"] == p.position_id)


def healed_trade(d, xid="h" * 32):
    """Design N-1 / T-A19: entry 4 @ 10.00, then 6 cumulative without a price; exit 6 @ 11.00 (6.00 as first written); the entry then ends
    canceled at 6 @ 10.20 - the heal re-prices the exit: 4.80. Returns (entry intent, a function applying the heal)."""
    ent = d.entry(xid=xid)
    d.observe(ent, "partially_filled", 4, "10.00")
    d.observe(ent, "partially_filled", 6, None)
    ex = d.exit(d.pos(ent), "x" + xid[1:], 6)
    d.observe(ex, "filled", 6, "11.00")
    return ent, lambda: d.observe(ent, "canceled", 6, "10.20")


def sessions(d, dates, status="HEALTHY"):
    for t in dates:
        d.fvs.open_session(t, T0, "test-owner", in_session=True)
        d.fvs.update_session(t, T0, session_status=status, ended_at=T0.isoformat(timespec="seconds"), market_close_seen=True)


def api(monkeypatch, d):
    monkeypatch.setitem(RT.STREAM, "instance", SimpleNamespace(forward_validation=d.fv, execution=d.x))
    from api.server import app
    return TestClient(app)


# ================================================================ T-A01 ... T-A24: the executor's accounting =================================

def test_TA01_one_exit_order_observed_twice_is_priced_from_its_cumulative_average(tmp_path):
    d = Direct(tmp_path)
    ent = d.entry()
    d.observe(ent, "partially_filled", 4, "10.00")
    d.observe(ent, "filled", 10, "10.30")
    ex = d.exit(d.pos(ent), "x" * 32, 10)
    d.observe(ex, "partially_filled", 4, "12.00")
    d.observe(ex, "filled", 10, "10.20")
    p = d.pos(ent)
    assert (p.state, p.average_entry_price, p.average_exit_price, p.realized_pnl) == ("CLOSED", "10.30", "10.20", "-1.00")   # baseline: 10.92 / +6.20
    assert d.fv.verification(p) == {"verified": True, "unverified_reasons": []}


def test_TA02_one_exit_order_observed_three_times(tmp_path):
    d = Direct(tmp_path)
    ent = d.entry()
    d.observe(ent, "filled", 10, "10.00")
    ex = d.exit(d.pos(ent), "x" * 32, 10)
    for q, a in ((2, "11.00"), (5, "10.40"), (10, "9.70")):                     # fills 2 @ 11, 3 @ 10, 5 @ 9 as the broker's cumulative view
        d.observe(ex, "filled" if q == 10 else "partially_filled", q, a)
    p = d.pos(ent)
    assert (p.average_exit_price, p.realized_pnl) == ("9.70", "-3.00")            # baseline 10.17


def test_TA03_a_restart_between_the_observations_gives_the_same_row(tmp_path):
    d = Direct(tmp_path)
    ent = d.entry()
    d.observe(ent, "partially_filled", 4, "10.00")
    d.observe(ent, "filled", 10, "10.30")
    ex = d.exit(d.pos(ent), "x" * 32, 10)
    d.observe(ex, "partially_filled", 4, "12.00")
    d.restart()
    d.observe(ex, "filled", 10, "10.20")
    p = d.pos(ent)
    assert (p.average_exit_price, p.realized_pnl) == ("10.20", "-1.00") and d.fv.verification(p)["verified"] is True


def test_TA04_two_exit_orders_the_second_observed_twice(tmp_path):
    d = Direct(tmp_path)
    ent = d.entry()
    d.observe(ent, "filled", 10, "10.00")
    ex1 = d.exit(d.pos(ent), "x" * 32, 10, cycle=1)
    d.observe(ex1, "partially_filled", 4, "12.00")
    d.observe(ex1, "canceled", 4, "12.00")
    assert d.pos(ent).state == "EXIT_REQUIRED" and d.pos(ent).quantity == 6        # the remainder handed back, as before
    ex2 = d.exit(d.pos(ent), "y" * 32, 6, cycle=2)
    d.observe(ex2, "partially_filled", 3, "9.00")
    d.observe(ex2, "filled", 6, "9.50")
    p = d.pos(ent)
    assert (p.state, p.exit_qty, p.average_exit_price, p.realized_pnl) == ("CLOSED", 10, money(Decimal("105") / 10), "5.00")   # (4*12 + 6*9.50) / 10


def _property_trace(seed):
    """One seeded position: the true fills (the oracle) and the broker's cumulative views of each order, some unpriced, some repeated."""
    rnd = random.Random(seed)

    def views(total, lo, hi):
        fills, obs, q, n = [], [], 0, Decimal(0)
        while q < total:
            dq = min(total - q, rnd.randint(1, max(1, total // 2)))
            px = Decimal(rnd.randint(int(lo * 100), int(hi * 100))) / 100
            q, n = q + dq, n + px * dq
            fills.append((dq, px))
            avg = S.cents(n / q)
            obs.append([q, None if (q < total and rnd.random() < 0.3) else avg])
            if rnd.random() < 0.25:
                obs.append([q, avg if obs[-1][1] is None else None])                 # a price-only heal, or a price-less repeat
        obs[-1][1] = S.cents(n / q)                                                    # the last view of a finished order carries its price
        return fills, obs, n
    efills, eobs, en = views(10, 9.5, 10.5)
    cycles, left = [], 10
    for c in (1, 2, 3):
        take = left if c == 3 or rnd.random() < 0.5 else rnd.randint(1, left)
        fills, obs, n = views(take, 9.0, 12.0)
        cycles.append((left, take, obs, n))
        left -= take
        if left == 0:
            break
    return efills, eobs, en, cycles


def _run_property(d, seed, tag, last_only):
    efills, eobs, en, cycles = _property_trace(seed)
    ent = d.entry(xid=f"{tag}{seed:031d}")
    closed_at = set()
    for i, (q, a) in enumerate(eobs[-1:] if last_only else eobs):
        d.observe(ent, "filled" if q == 10 and i == (0 if last_only else len(eobs) - 1) else "partially_filled", q, a)
    for c, (qty, take, obs, n) in enumerate(cycles, start=1):
        ex = d.exit(d.pos(ent), f"{tag}{seed:027d}x{c:03d}", qty, cycle=c)
        seq = obs[-1:] if last_only else obs
        for i, (q, a) in enumerate(seq):
            final = i == len(seq) - 1
            d.observe(ex, ("filled" if take == qty else "canceled") if final else "partially_filled", q, a)
            p = d.pos(ent)
            if p.closed_at:
                closed_at.add(p.closed_at)
    p = d.pos(ent)
    xn = sum(n for _, _, _, n in cycles)
    return p, en / 10, xn, closed_at


def test_TA05_property_cumulative_views_every_or_last_observation_and_the_per_fill_oracle(tmp_path):
    d = Direct(tmp_path)
    verified = 0
    for seed in range(200):
        p, e_true, n_true, closed_at = _run_property(d, seed, "a", last_only=False)
        q, _, _, _ = _run_property(d, seed, "b", last_only=True)
        cols = ("state", "entry_qty", "exit_qty", "average_entry_price", "average_exit_price", "realized_pnl")
        assert {k: getattr(p, k) for k in cols} == {k: getattr(q, k) for k in cols}, seed   # independent of the observation sequence
        assert len(closed_at) <= 1, (seed, closed_at)                                          # closed_at never changes once set
        if d.fv.verification(p)["verified"]:
            verified += 1
            real = Decimal(p.realized_pnl)
            assert abs(real - (n_true - e_true * p.exit_qty)) <= Decimal("0.01") * p.exit_qty + Decimal("0.005"), seed
            assert abs(Decimal(p.average_exit_price) - n_true / p.exit_qty) <= Decimal("0.01"), seed
    assert verified >= 150, verified


def test_TA06_the_same_observation_applied_twice_changes_nothing(tmp_path):
    d = Direct(tmp_path)
    p0 = d.closed_trade(d.entry(), "10.00", "10.20")
    ev0 = d.xs.position_events(p0.position_id)
    ex = d.xs.exit_intents_for_position(p0.position_id)[0]
    d.observe(ex, "filled", 10, "10.20")
    assert d.xs.position(p0.position_id) == p0 and d.xs.position_events(p0.position_id) == ev0


def test_TA07_an_exit_fill_without_a_price_applies_the_quantity_and_leaves_the_pnl_null(tmp_path):
    d = Direct(tmp_path)
    ent = d.entry()
    d.observe(ent, "filled", 10, "10.00")
    ex = d.exit(d.pos(ent), "x" * 32, 10)
    d.observe(ex, "filled", 10, None)
    p, it = d.pos(ent), d.xs.intent(ex.execution_id)
    assert (p.state, p.quantity, p.exit_qty, p.average_exit_price, p.realized_pnl) == ("CLOSED", 0, 10, None, None)   # baseline: -100.00
    assert it.state == "FILLED" and it.broker_status == "filled"                          # broker-mapped, never overridden
    assert {"PNL_NULL", "EXIT_PRICE_MISSING"} <= set(reasons(d, p))
    snap = d.xs.position_events(p.position_id)[-1]["detail"]["pnl_snapshot"]
    assert snap["valid"] is False and "error" not in snap                                  # invalid by the rule, not by an exception
    z = d.entry(xid="z" * 32)                                                                # a fill reported at a ZERO price is no price either
    d.observe(z, "filled", 10, "10.00")
    d.observe(d.exit(d.pos(z), "y" * 32, 10), "filled", 10, "0.00")
    q = d.pos(z)
    assert (q.state, q.average_exit_price, q.realized_pnl) == ("CLOSED", None, None) and "EXIT_PRICE_MISSING" in reasons(d, q)


def test_TA08_an_entry_fill_without_a_price_still_opens_the_position_and_its_stop_is_checked(tmp_path):
    st = make_stack(tmp_path)
    pv = arm(st)
    st.sim.auto_fill = "manual"
    cyc(st)
    it = st.x.store.intents(intent_type="ENTRY")[0]
    st.sim.fill(it.client_order_id)
    st.sim.order_by_coid(it.client_order_id)["filled_avg_price"] = None
    cyc(st)
    pos = st.x.store.open_position("SNDK")
    assert pos is not None and pos.quantity == pv.final_qty and pos.average_entry_price == "0.00" and st.x.store.intent(it.execution_id).state == "FILLED"
    below_invalidation(st, pv)
    cyc(st)
    assert st.x.store.intents(intent_type="EXIT"), "the stop was evaluated: an exit was decided and submitted"
    for i in st.x.store.open_intents():
        st.sim.fill(i.client_order_id)
    cyc(st)
    fv = ForwardValidationService(stream_fn=lambda: SimpleNamespace(execution=st.x), store=ForwardValidationStore(st.db))
    v = fv.verification(st.x.store.position(pos.position_id))
    assert v["verified"] is False and "ENTRY_PRICE_INVALID" in v["unverified_reasons"]


def test_TA09_an_accounting_read_failure_never_blocks_the_quantity_or_the_cycle(tmp_path, monkeypatch, caplog):
    st = make_stack(tmp_path)
    pv, pos = entered(st)
    st.sim.auto_fill = "manual"
    below_invalidation(st, pv)
    cyc(st)
    ex = st.x.store.intents(intent_type="EXIT")[0]

    def boom(*a, **k):
        raise RuntimeError("injected accounting read failure")
    monkeypatch.setattr(st.x.store, "exit_intents_for_position", boom)
    st.sim.fill(ex.client_order_id)
    out = cyc(st)
    assert out["result"] == "RUN" and "exits_submitted" in out["summary"], out                 # G-12a: the cycle ran and evaluated the exits
    p = st.x.store.position(pos.position_id)
    assert (p.state, p.quantity, p.exit_qty, p.realized_pnl, p.average_exit_price) == ("CLOSED", 0, pos.quantity, None, None)
    snap = st.x.store.position_events(pos.position_id)[-1]["detail"]["pnl_snapshot"]
    assert snap["valid"] is False and snap["error"] == "RuntimeError" and "exit accounting" in caplog.text


def test_TA09b_an_entry_branch_accounting_failure_applies_the_quantity_keeps_the_prior_price_and_stays_unverified(tmp_path, monkeypatch, caplog):
    """Checkpoint 1 K-04 (G-12c), with checkpoint 2 L-01: with and without exits already filled, a failed recompute on an entry fill still writes the
    quantity, keeps the prior entry price (NOT NULL) and leaves the exit P/L NULL with an invalid snapshot equal to the row. The intent keeps the
    BROKER's (Q, A) - never a pair the broker did not report - so the trade reads UNVERIFIED (ENTRY_MISMATCH) and never verifies a stale price."""
    import execution.service as XS
    d = Direct(tmp_path)
    ent, heal = healed_trade(d)                                                              # exit_qty > 0: the heal path
    monkeypatch.setattr(d.xs, "exit_intents_for_position", lambda pid: (_ for _ in ()).throw(sqlite3.OperationalError("database is locked")))
    heal()
    p = d.pos(ent)
    snap = d.xs.position_events(p.position_id)[-1]["detail"]["pnl_snapshot"]
    assert (p.state, p.quantity, p.entry_qty, p.average_entry_price, p.realized_pnl) == ("CLOSED", 0, 6, "10.00", None) and "entry accounting" in caplog.text
    assert snap["valid"] is False and (snap["average_entry_price"], snap["realized_pnl"], snap["exit_qty"]) == (p.average_entry_price, None, p.exit_qty)
    assert (d.xs.intent(ent.execution_id).filled_qty, d.xs.intent(ent.execution_id).filled_avg_price) == (6, "10.20")   # L-01: what the broker reported
    assert "ENTRY_MISMATCH" in reasons(d, p) and "PNL_NULL" in reasons(d, p)
    d2 = Direct(tmp_path / "noexit")                                                         # exit_qty == 0: the recompute itself raises
    e2 = d2.entry()
    d2.observe(e2, "partially_filled", 4, "10.00")
    real = XS.recompute_pnl
    monkeypatch.setattr(XS, "recompute_pnl", lambda *a, **k: (_ for _ in ()).throw(ArithmeticError("injected")))
    d2.observe(e2, "filled", 10, "10.40")
    q = d2.pos(e2)
    assert (q.quantity, q.entry_qty, q.average_entry_price) == (10, 10, "10.00") and d2.xs.position_events(q.position_id)[-1]["detail"]["pnl_snapshot"]["valid"] is False
    assert d2.xs.intent(e2.execution_id).filled_avg_price == "10.40"
    monkeypatch.setattr(XS, "recompute_pnl", real)
    d2.observe(d2.exit(q, "x" * 32, 10), "filled", 10, "11.00")                              # a later exit: priced, but the entry evidence disagrees
    q = d2.pos(e2)
    assert q.state == "CLOSED" and d2.fv.verification(q)["verified"] is False and "ENTRY_MISMATCH" in reasons(d2, q)   # never verified at the stale 10.00


def test_TA09c_an_exit_accounting_failure_on_an_orders_last_view_keeps_the_broker_view_so_a_later_recompute_is_true(tmp_path, monkeypatch):
    """Checkpoint 2 L-01: the exit order's terminal view (canceled, 10 cumulative @ 10.50) is applied while the accounting read fails. The intent
    keeps (10, 10.50) exactly as reported, so the next exit order's recompute prices the trade from the true fills: 15.00, verified."""
    from tools import verify_exit_fills as V
    d = Direct(tmp_path)
    ent = d.entry(qty=20)
    d.observe(ent, "filled", 20, "10.00")
    x1 = d.exit(d.pos(ent), "x" * 32, 20, cycle=1)
    d.observe(x1, "partially_filled", 5, "10.00")
    real_x = d.xs.exit_intents_for_position
    monkeypatch.setattr(d.xs, "exit_intents_for_position", lambda pid: (_ for _ in ()).throw(sqlite3.OperationalError("database is locked")))
    d.observe(x1, "canceled", 10, "10.50")
    monkeypatch.setattr(d.xs, "exit_intents_for_position", real_x)
    p = d.pos(ent)
    assert (p.state, p.quantity, p.exit_qty, p.realized_pnl) == ("EXIT_REQUIRED", 10, 10, None)
    assert (d.xs.intent(x1.execution_id).filled_qty, d.xs.intent(x1.execution_id).filled_avg_price) == (10, "10.50")   # the broker's view, not a stale pair
    d.observe(d.exit(p, "y" * 32, 10, cycle=2), "filled", 10, "11.00")
    p = d.pos(ent)
    assert (p.state, p.realized_pnl) == ("CLOSED", "15.00") and d.fv.verification(p)["verified"] is True     # 10 x 10.50 + 10 x 11.00 - 20 x 10.00
    assert V.check(d.db)["counts"] == {"MATCH": 1, "DIFFERS": 0, "CANNOT_REDERIVE": 0}


def test_TA10_the_stage_61_statistics_of_the_corrected_trade(tmp_path):
    d = Direct(tmp_path)
    ent = d.entry()
    d.observe(ent, "partially_filled", 4, "10.00")
    d.observe(ent, "filled", 10, "10.30")
    ex = d.exit(d.pos(ent), "x" * 32, 10)
    d.observe(ex, "partially_filled", 4, "12.00")
    d.observe(ex, "filled", 10, "10.20")
    wl = d.fv.daily_summary(DAY)["win_loss"]
    assert (wl["win_count"], wl["loss_count"], wl["expectancy"], wl["gross_pnl"]) == (0, 1, "-1.00", "-1.00")   # baseline: one WIN of +6.20
    st = d.fv.cumulative()["strategy"]
    assert (st["loss_count"], st["win_count"], st["unverified"]) == (1, 0, 0)


def test_TA11_a_closed_row_whose_exit_quantity_differs_from_its_entry_is_unverified(tmp_path):
    d = Direct(tmp_path)
    p = d.closed_trade(d.entry())
    with sqlite3.connect(d.db) as c:
        c.execute("UPDATE paper_positions SET exit_qty = 9 WHERE position_id = ?", (p.position_id,))
    assert "QTY_MISMATCH" in reasons(d, d.xs.position(p.position_id))


def test_TA12_a_null_realized_pnl_is_unverified_and_never_replaced_by_the_gross(tmp_path):
    d = Direct(tmp_path)
    p = d.closed_trade(d.entry(), "10.00", "11.00")
    with sqlite3.connect(d.db) as c:
        c.execute("UPDATE paper_positions SET realized_pnl = NULL WHERE position_id = ?", (p.position_id,))
    t = trade_of(d, p)
    assert "PNL_NULL" in t["unverified_reasons"] and t["realized_pnl"] is None and t["gross_pnl"] is None and t["recorded_pnl"]["gross_pnl"] == "10.00"
    s = d.fv.daily_summary(DAY)
    assert s["realized_pnl"] is None and s["win_loss"]["closed_trades"] == 0 and s["unverified"] == 1          # baseline: the gross 10.00 counted


def test_TA13_a_partial_entry_and_an_unpriced_increment_heal_on_the_priced_fill(tmp_path):
    d = Direct(tmp_path)
    ent = d.entry()
    d.observe(ent, "partially_filled", 4, "10.00")
    d.observe(ent, "partially_filled", 7, None)
    assert d.pos(ent).average_entry_price == "10.00" and d.xs.intent(ent.execution_id).filled_avg_price is None
    d.observe(ent, "filled", 10, "10.30")
    assert d.pos(ent).average_entry_price == "10.30"
    ex = d.exit(d.pos(ent), "x" * 32, 10)
    d.observe(ex, "filled", 10, "11.00")
    p = d.pos(ent)
    assert p.realized_pnl == "7.00" and d.fv.verification(p)["verified"] is True


def test_TA14_an_exit_price_arriving_at_the_same_quantity_reruns_the_accounting(tmp_path):
    d = Direct(tmp_path)
    ent = d.entry()
    d.observe(ent, "filled", 10, "10.00")
    ex = d.exit(d.pos(ent), "x" * 32, 10)
    d.observe(ex, "partially_filled", 6, None)
    assert d.pos(ent).average_exit_price is None and d.pos(ent).realized_pnl is None
    d.observe(ex, "partially_filled", 6, "11.00")                                   # price only: baseline never re-ran the accounting
    assert (d.pos(ent).average_exit_price, d.pos(ent).realized_pnl) == ("11.00", "6.00")
    d.observe(ex, "filled", 10, "11.00")
    p = d.pos(ent)
    assert p.realized_pnl == "10.00" and d.fv.verification(p)["verified"] is True


def test_TA15_an_unpriced_canceled_partial_exit_hands_the_remainder_back_and_stays_evidence_only(tmp_path):
    d = Direct(tmp_path)
    ent = d.entry()
    d.observe(ent, "filled", 10, "10.00")
    ex1 = d.exit(d.pos(ent), "x" * 32, 10, cycle=1)
    d.observe(ex1, "canceled", 4, None)
    p = d.pos(ent)
    assert (p.state, p.quantity, p.exit_qty, p.realized_pnl) == ("EXIT_REQUIRED", 6, 4, None)                     # hand-back unchanged (D-W2-f)
    ex2 = d.exit(p, "y" * 32, 6, cycle=2)
    d.observe(ex2, "filled", 6, "10.50")
    p = d.pos(ent)
    assert p.state == "CLOSED" and p.realized_pnl is None and "EXIT_PRICE_MISSING" in reasons(d, p)


def test_TA16_golden_decisions_unchanged_on_the_real_stack(tmp_path):
    """16 executor traces through the real stack: intents submitted, exit decisions, intent states, hand-backs and reconciliation output are
    identical to 5d6f7dd (tests/wave2_pra_golden.json, generated on the baseline)."""
    from test_execution_60 import make_stack as mk
    for fn in S.stack_scenarios():
        st = mk(tmp_path / fn.__name__)
        snaps = []
        fn(st, lambda: snaps.append(S.stack_snapshot(st)))
        assert {"steps": len(snaps), "sha256": S.trace_digest(snaps)} == GOLDEN["stack"][fn.__name__], fn.__name__
        if fn.__name__ == "s05":                                                     # the price-only trigger ran there: the correction re-priced the open exit
            p = st.x.store.positions(open_only=False)[0]
            ex = st.x.store.intents(intent_type="EXIT")[0]
            assert any(e["detail"].get("average_exit_price") == "49.50" for e in st.x.store.position_events(p.position_id) if e["kind"] == "EXIT_FILL")
            assert p.average_exit_price == st.sim.order_by_coid(ex.client_order_id)["filled_avg_price"]       # one order: its own cumulative average
        if fn.__name__ == "s08":                                                     # a terminal fill without a price is never re-observed: it stays evidence only
            p = st.x.store.positions(open_only=False)[0]
            assert p.average_entry_price == "0.00" and st.x.store.intent(p.execution_id).state == "FILLED"


def test_TA17_a_decreasing_then_rising_fill_quantity_leaves_the_pnl_null(tmp_path):
    d = Direct(tmp_path)
    ent = d.entry()
    d.observe(ent, "filled", 10, "10.00")
    ex = d.exit(d.pos(ent), "x" * 32, 10)
    d.observe(ex, "partially_filled", 6, "11.00")
    d.observe(ex, "partially_filled", 4, "11.00")                                   # the broker's quantity went DOWN
    d.observe(ex, "filled", 10, "11.00")
    p = d.pos(ent)
    assert p.realized_pnl is None and p.average_exit_price is None, p                 # J-07c: sum(Q_o) != exit_qty invalidates the P/L itself
    assert d.fv.verification(p)["verified"] is False


def test_TA18_three_exit_cycles_each_observed_once_match_the_oracle(tmp_path):
    d = Direct(tmp_path)
    ent = d.entry()
    d.observe(ent, "filled", 10, "10.00")
    for c, (qty, take, px, xid) in enumerate(((10, 3, "10.10", "x"), (7, 3, "9.95", "y"), (4, 4, "10.03", "z")), start=1):
        ex = d.exit(d.pos(ent), xid * 32, qty, cycle=c)
        d.observe(ex, "filled" if take == qty else "canceled", take, px)
    p = d.pos(ent)
    n = Decimal("3") * Decimal("10.10") + 3 * Decimal("9.95") + 4 * Decimal("10.03")
    assert (p.average_exit_price, p.realized_pnl) == (money(n / 10), money(n - 100)) == ("10.03", "0.27")   # the oracle; baseline 0.24


def test_TA19_an_entry_heal_reprices_the_closed_exit(tmp_path):
    d = Direct(tmp_path)
    ent, heal = healed_trade(d)
    assert d.pos(ent).realized_pnl == "6.00" and d.pos(ent).state == "CLOSED"
    heal()
    p = d.pos(ent)
    assert (p.state, p.average_entry_price, p.realized_pnl) == ("CLOSED", "10.20", "4.80") and d.fv.verification(p)["verified"] is True


def test_TA20_a_recompute_on_a_closed_position_keeps_exactly_one_closed_event(tmp_path):
    d = Direct(tmp_path)
    p0 = d.closed_trade(d.entry(), "10.30", "10.20")
    ex = d.xs.exit_intents_for_position(p0.position_id)[0]
    d.observe(ex, "filled", 10, "10.25")                                            # a late price correction of the filled exit
    p = d.xs.position(p0.position_id)
    ev = d.xs.position_events(p0.position_id)
    assert p.realized_pnl == "-0.50" and p.closed_at == p0.closed_at and [e["kind"] for e in ev].count("CLOSED") == 1
    assert (ev[-1]["kind"], ev[-1]["from_state"], ev[-1]["to_state"]) == ("EXIT_FILL", "CLOSED", "CLOSED")


def test_TA21_a_row_consistent_with_its_snapshot_but_not_with_its_intents_is_unverified(tmp_path):
    d = Direct(tmp_path)
    p = d.closed_trade(d.entry(), "10.00", "10.20")
    ex = d.xs.exit_intents_for_position(p.position_id)[0]
    d.xs.record_order(order(d.oids[ex.execution_id], ex.client_order_id, "SNDK", "sell", 10, "filled", 10, "10.40"), ex.execution_id, d.tick(), "monitor",
                      intent_fields={"filled_avg_price": "10.40"})                 # J-07b: a rollback-style price-only write, no fill accounting
    assert reasons(d, d.xs.position(p.position_id)) == ["REDERIVATION_MISMATCH"]       # the snapshot still equals the row: only the re-derivation sees it


def test_TA22_a_trade_closed_while_its_entry_is_still_working_never_verifies(tmp_path):
    d = Direct(tmp_path)
    ent = d.entry()
    d.observe(ent, "partially_filled", 6, "10.00")
    ex = d.exit(d.pos(ent), "x" * 32, 6)
    d.observe(ex, "filled", 6, "11.00")
    p = d.pos(ent)
    assert p.state == "CLOSED" and "INTENT_OPEN" in reasons(d, p)                    # baseline: verified True, then False
    d.observe(ent, "filled", 10, "10.00")
    assert d.fv.verification(d.pos(ent))["verified"] is False


def test_TA23_every_fill_event_carries_the_snapshot_and_the_latest_equals_the_row(tmp_path):
    d = Direct(tmp_path)
    a = d.closed_trade(d.entry(xid="a" * 32), "10.00", "10.20")
    ent, heal = healed_trade(d, xid="b" * 32)
    heal()
    flat = d.entry(xid="c" * 32)
    d.observe(flat, "partially_filled", 5, "10.00")
    d.observe(flat, "filled", 10, "10.00")                                        # a same-price entry increment
    fx = d.exit(d.pos(flat), "d" * 32, 10)
    d.observe(fx, "filled", 10, "10.00")                                          # a flat trade
    for p in d.xs.positions(open_only=False):
        evs = d.xs.position_events(p.position_id)
        fill_events = [e for e in evs if e["kind"] in ("OPENED", "FILL_UPDATED", "EXIT_FILL", "CLOSED")]
        assert fill_events and all("pnl_snapshot" in e["detail"] for e in fill_events), p.position_id
        snap = fill_events[-1]["detail"]["pnl_snapshot"]
        assert {k: snap[k] for k in ("entry_qty", "average_entry_price", "exit_qty", "average_exit_price", "realized_pnl")} == \
               {k: getattr(p, k) for k in ("entry_qty", "average_entry_price", "exit_qty", "average_exit_price", "realized_pnl")}, p.position_id
    h = d.pos(ent)
    last = d.xs.position_events(h.position_id)[-1]
    assert last["kind"] == "FILL_UPDATED" and last["detail"]["pnl_snapshot"]["realized_pnl"] == "4.80"
    assert d.pos(flat).realized_pnl == "0.00" and d.fv.verification(d.pos(flat))["verified"] is True and a.realized_pnl == "2.00"


def test_TA23b_a_latest_snapshot_that_disagrees_with_the_row_is_unverified(tmp_path):
    """F-04 / E3: the row and its intents agree (the re-derivation holds), but the latest snapshot-bearing event says otherwise: UNVERIFIED."""
    d = Direct(tmp_path)
    p = d.closed_trade(d.entry(), "10.00", "11.00")
    assert d.fv.verification(p)["verified"] is True
    detail = {"pnl_snapshot": {"entry_qty": 10, "average_entry_price": "10.00", "exit_qty": 10, "average_exit_price": "11.00", "realized_pnl": "9.99", "valid": True,
                               "basis": "W2.recompute.v1"}}
    with sqlite3.connect(d.db) as c:                                                           # an appended event, as a torn or foreign write would leave it
        c.execute("INSERT INTO position_events (event_id, position_id, at, kind, from_state, to_state, reason, detail_json) VALUES (?,?,?,?,?,?,?,?)",
                  ("e" * 32, p.position_id, d.tick().isoformat(timespec="seconds"), "EXIT_FILL", "CLOSED", "CLOSED", "test", json.dumps(detail)))
    assert reasons(d, d.xs.position(p.position_id)) == ["SNAPSHOT_MISMATCH"]


def test_TA24_a_price_flapping_broker_keeps_the_stored_price_and_writes_no_recompute(tmp_path):
    d = Direct(tmp_path)
    ent = d.entry()
    d.observe(ent, "filled", 10, "10.00")
    ex = d.exit(d.pos(ent), "x" * 32, 10)
    d.observe(ex, "partially_filled", 6, "10.50")
    n0 = len(d.xs.position_events(d.pos(ent).position_id))
    for i in range(10):
        d.observe(ex, "partially_filled", 6, None if i % 2 == 0 else "10.50")
        assert d.xs.intent(ex.execution_id).filled_avg_price == "10.50"
    assert len(d.xs.position_events(d.pos(ent).position_id)) - n0 <= 1               # baseline: +5 events and a flipping verdict
    d.observe(ex, "filled", 10, "10.50")
    assert d.fv.verification(d.pos(ent))["verified"] is True


# ================================================================ T-A25 ... T-A35: fresh, read-only summaries ===============================

def _five_days_current(d):
    sessions(d, DATES)
    for t in DATES:
        d.fvs.put_daily(t, d.t, FV_VERSION, d.fv.daily_summary(t))                   # as the session close stores them (no watermark)
    for _ in range(3):
        d.fv._persist_stale_summaries(d.tick())
    assert all(d.fvs.daily(t).get("pnl_watermark") == d.fv.pnl_watermark(t) for t in DATES)


def test_TA25_a_heal_after_the_close_is_served_by_every_read_site_and_persisted_once(tmp_path, monkeypatch):
    d = Direct(tmp_path)
    ent, heal = healed_trade(d)
    _five_days_current(d)
    stored = d.fvs.daily(DAY)
    assert (stored["pnl_status"], stored["realized_pnl"]) == ("UNVERIFIED_PRESENT", None)             # before the heal the entry was still open: fail-closed, not 6.00
    heal()
    real_block = d.fv._run_block                                                            # Wave 2 PR C (C-88): the weekly gate counts a run's member sessions;
    monkeypatch.setattr(d.fv, "_run_block", lambda now=None: (lambda b: {**b, "defined": True, "sessions": {**b["sessions"], "member_dates": list(DATES)}})(real_block(now)))   # here the five days are
    c = api(monkeypatch, d)
    before = content_hash(d.db)
    j = {"session": c.get(f"/api/forward-validation/sessions/{DAY}").json()["daily"], "daily": c.get("/api/forward-validation/daily").json()["daily"][0],
         "day": c.get(f"/api/forward-validation/daily/{DAY}").json()}
    text = {"latest": c.get("/api/forward-validation/report/daily").text, "day": c.get(f"/api/forward-validation/report/daily/{DAY}").text,
            "weekly": c.get("/api/forward-validation/report/weekly").text}
    csv = c.get("/api/forward-validation/export/daily?format=csv").text
    assert content_hash(d.db) == before                                                     # (ii) the reads wrote nothing
    assert all(v["trading_date"] == DAY and v["realized_pnl"] == "4.80" for v in j.values()), j
    assert all("4.80" in t and "6.00" not in t for t in text.values()), text
    row = [ln for ln in csv.splitlines() if ln.startswith(DAY)][0]
    assert ",4.80," in row and "6.00" not in row
    out = d.fv._persist_stale_summaries(d.tick())                                            # (iii) the next tick persists it once
    assert out["rebuilt"] == 1 and len(d.fvs.events(kind="SUMMARY_REBUILT")) == 1 and d.fvs.daily(DAY)["realized_pnl"] == "4.80"
    for _ in range(2):                                                                       # (iv) idempotent
        d.fv._persist_stale_summaries(d.tick())
        c.get(f"/api/forward-validation/daily/{DAY}")
    assert len(d.fvs.events(kind="SUMMARY_REBUILT")) == 1
    legacy = {k: v for k, v in d.fv.daily_summary(DAY).items() if k not in ("pnl_status", "unverified", "pnl_watermark")}
    d.fvs.put_daily(DAY, d.t, FV_VERSION, {**legacy, "realized_pnl": "6.00"})                # (v) a row as 5d6f7dd persisted it: 6.00, no watermark
    assert c.get(f"/api/forward-validation/daily/{DAY}").json()["realized_pnl"] == "4.80" and "6.00" not in c.get(f"/api/forward-validation/report/daily/{DAY}").text


def test_TA26_every_exit_intent_is_read_from_one_transaction(tmp_path):
    d = Direct(tmp_path)
    first = d.closed_trade(d.entry(xid=f"{0:032d}"), xid=f"x{0:031d}")
    for n in range(1, 60):
        d.closed_trade(d.entry(xid=f"{n:032d}"), xid=f"x{n:031d}")
    assert all(i.position_id != first.position_id for i in d.xs.intents("SNDK", intent_type="EXIT", limit=50))   # the old LIMIT 50 read misses it
    assert d.fv.verification(first)["verified"] is True
    ex = d.xs.exit_intents_for_position(first.position_id)[0]
    stop, torn, ok = threading.Event(), [], []

    def writer():                                                                          # K-02: real corrections of THIS trade (position, intent, snapshot all move)
        k = 0
        while not stop.is_set():
            k += 1
            d.observe(ex, "filled", 10, "11.00" if k % 2 else "11.10", minutes=0)
    th = threading.Thread(target=writer)
    th.start()
    try:
        for _ in range(150):
            v = d.fv.verification(d.xs.position(first.position_id))
            ok.append(v["verified"])
            if set(v["unverified_reasons"]) - {"VERIFY_ERROR"}:                         # a row read after the caller's (K-01) is allowed; a torn VIEW never is
                torn.append(v)
    finally:
        stop.set()
        th.join()
    assert torn == [] and any(ok), torn[:3]


def test_TA27_the_statistics_use_verified_trades_only(tmp_path):
    d = Direct(tmp_path)
    d.closed_trade(d.entry(xid="a" * 32), "10.00", "11.00")
    bad = d.entry(xid="b" * 32)
    d.observe(bad, "filled", 10, "10.00")
    d.observe(d.exit(d.pos(bad), "c" * 32, 10), "filled", 10, None)
    s = d.fv.daily_summary(DAY)
    assert (s["realized_pnl"], s["win_loss"]["closed_trades"], s["unverified"], s["pnl_status"]) == ("10.00", 1, 1, "UNVERIFIED_PRESENT")
    cs = d.fv.cumulative()["strategy"]
    assert (cs["closed_trades"], cs["gross_pnl"], cs["unverified"]) == (1, "10.00", 1)
    amb = d.closed_trade(d.entry(xid="d" * 32), "10.00", "12.00")                          # K-03: an unverified trade WITH a raw P/L (20.00)
    d.xs.update_intent("d" * 32, d.tick(), outcome_class="O7")
    assert d.xs.position(amb.position_id).realized_pnl == "20.00"
    cs = d.fv.cumulative()["strategy"]
    assert (cs["closed_trades"], cs["gross_pnl"], cs["unverified"], cs["win_count"]) == (1, "10.00", 2, 1)
    s = d.fv.daily_summary(DAY)
    assert (s["realized_pnl"], s["unverified"]) == ("10.00", 2)


def test_TA46_a_write_between_the_trade_read_and_its_verification_is_never_shown_as_verified(tmp_path, monkeypatch):
    """Checkpoint 1 K-01: closed_trades() reads the rows once; a heal that commits before a trade's verification must not let the earlier values
    pass as verified. The heal is injected deterministically between the two reads (inside _trade, at the marks read)."""
    def torn(name):
        d = Direct(tmp_path / name)
        ent, heal = healed_trade(d)
        real, fired = d.fvs.marks, []

        def marks(pid, limit=20000):
            if not fired:
                fired.append(1)
                heal()                                                                     # the entry ends canceled at 10.20: the row becomes 4.80, verified
            return real(pid, limit)
        monkeypatch.setattr(d.fvs, "marks", marks)
        return d, ent
    d, ent = torn("closed")
    t = d.fv.closed_trades()[0]
    assert d.pos(ent).realized_pnl == "4.80" and t["recorded_pnl"]["realized_pnl"] == "6.00"     # the values read BEFORE the heal
    assert (t["verified"], t["unverified_reasons"], t["realized_pnl"], t["gross_pnl"]) == (False, ["VERIFY_ERROR"], None, None)
    assert d.fv.closed_trades()[0]["realized_pnl"] == "4.80"                                       # the next read: consistent, verified
    d2, _ = torn("cumulative")
    cs = d2.fv.cumulative()["strategy"]
    assert (cs["gross_pnl"], cs["unverified"]) == (None, 1)                                         # never 6.00 counted as verified
    d3, _ = torn("summary")
    s = d3.fv.bracketed_summary(DAY)
    assert (s["pnl_status"], s["realized_pnl"]) == ("OK", "4.80")                                   # the bracket retried: one consistent snapshot


def test_TA28_property_decision_fields_are_identical_to_the_baseline(tmp_path):
    """150 seeded traces (the A9 shape, with unpriced and price-only observations): every decision field after every step equals 5d6f7dd's."""
    got = S.decision_golden(tmp_path, range(150))
    assert got == GOLDEN["decision"], [s for s in got if got[s] != GOLDEN["decision"].get(s)][:10]
    assert sum(v["steps"] for v in got.values()) > 1000


def test_TA29_every_get_route_and_export_is_read_only(tmp_path, monkeypatch):
    d = Direct(tmp_path)
    ent, heal = healed_trade(d)
    _five_days_current(d)
    heal()                                                                                    # stale rows
    legacy = {k: v for k, v in d.fv.daily_summary("2026-10-19").items() if k not in ("pnl_status", "unverified")}
    d.fvs.put_daily("2026-10-19", d.t, FV_VERSION, legacy)                                   # a legacy row
    c = api(monkeypatch, d)
    from forward_validation import EXPORTS
    paths = ["/api/forward-validation", f"/api/forward-validation/sessions/{DAY}", "/api/forward-validation/daily", f"/api/forward-validation/daily/{DAY}",
             "/api/forward-validation/daily/2026-10-19", "/api/forward-validation/report/daily", f"/api/forward-validation/report/daily/{DAY}",
             "/api/forward-validation/report/weekly", "/api/forward-validation/closed"] + [f"/api/forward-validation/export/{k}?format={f}" for k in EXPORTS for f in ("csv", "json")]
    held = sqlite3.connect(f"file:{d.db.as_posix()}?mode=ro", uri=True)
    try:
        for path in paths:
            h0, v0 = content_hash(d.db)[0], held.execute("PRAGMA data_version").fetchone()[0]
            r = c.get(path)
            assert r.status_code == 200, (path, r.text[:200])
            assert content_hash(d.db)[0] == h0 and held.execute("PRAGMA data_version").fetchone()[0] == v0, path
    finally:
        held.close()
    src = (ROOT / "forward_validation" / "service.py").read_text(encoding="utf-8")
    cls = next(n for n in ast.parse(src).body if isinstance(n, ast.ClassDef) and n.name == "ForwardValidationService")
    writers = {"put_daily", "add_event", "ensure", "_rw", "_write", "touch_control", "_touch"}
    for fn in (n for n in cls.body if isinstance(n, ast.FunctionDef) and n.name in ("fresh_daily", "fresh_dailies", "bracketed_summary", "pnl_watermark", "_pnl_status", "_null_pnl")):
        called = {getattr(c.func, "attr", getattr(c.func, "id", None)) for c in ast.walk(fn) if isinstance(c, ast.Call)}
        assert not called & writers, (fn.name, called & writers)


def _db_rows(db):
    """The database CONTENT a read must leave unchanged: sqlite_master and every table's rows (all columns), each read on its own mode=ro
    connection. Content, not file bytes: a WAL checkpoint may rewrite the file without changing a row."""
    conn = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True)
    try:
        out = {"sqlite_master": sorted(map(repr, conn.execute("SELECT type, name, tbl_name, sql FROM sqlite_master")))}
        for (name,) in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name").fetchall():
            out[name] = sorted(map(repr, conn.execute(f'SELECT * FROM "{name}"')))
        return out
    finally:
        conn.close()


def test_TA29b_every_read_and_export_path_running_pr_a_code_leaves_the_database_contents_unchanged(tmp_path, monkeypatch):
    """Final PR review Q-02: T-A29 extended to every user-facing path that runs a function PR A changed, with unverified trades present -
    every GET route of the forward-validation router (asserted against the router itself; /closed/{id} for each position), every export kind
    (the 6.10A capture kinds too: export_rows is a PR A function) as CSV and JSON, and the two strategy-registry routes (runtime_facts reads
    overview(): cumulative, closed_trades, verification). Each request is bracketed by every table's rows plus sqlite_master and by PRAGMA
    data_version on one connection held open across it. The fail-closed presentation is never written back: the unverified trades' stored raw
    values stay as they are while every read serves them null."""
    d = Direct(tmp_path)
    ok, amb, zero = _three_trades(d)                                                         # verified; INTENT_AMBIGUOUS with a raw 20.00; ENTRY_PRICE_INVALID
    ent, heal = healed_trade(d)
    _five_days_current(d)
    heal()                                                                                    # verified at 4.80; DAY's stored summary is stale
    legacy = {k: v for k, v in d.fv.daily_summary("2026-10-19").items() if k not in ("pnl_status", "unverified")}
    d.fvs.put_daily("2026-10-19", d.t, FV_VERSION, legacy)                                   # a legacy row
    nop = d.entry(xid="f" * 32)
    d.observe(nop, "filled", 10, "10.00")
    d.observe(d.exit(d.pos(nop), "9" * 32, 10), "filled", 10, None)                          # EXIT_PRICE_MISSING: stored NULL, after the summaries
    opn = d.entry(xid="e" * 32)
    d.observe(opn, "filled", 10, "10.00")                                                    # an open position
    pids = {"ok": ok.position_id, "healed": d.pos(ent).position_id, "amb": amb.position_id, "zero": zero.position_id, "nop": d.pos(nop).position_id,
            "open": d.pos(opn).position_id}
    raw = {k: d.xs.position(p).realized_pnl for k, p in pids.items()}
    assert (raw["amb"], raw["nop"], raw["ok"], raw["healed"]) == ("20.00", None, "10.00", "4.80")
    stored_day, rebuilt = d.fvs.daily(DAY), len(d.fvs.events(kind="SUMMARY_REBUILT"))
    c = api(monkeypatch, d)
    from api.routes import forward_validation as FVR
    from forward_capture import EXPORT_KINDS as CAPTURE_EXPORTS
    from forward_validation import EXPORTS
    fv = "/api/forward-validation"
    paths = ([fv, f"{fv}/sessions", f"{fv}/closed", f"{fv}/closed?date={DAY}", f"{fv}/daily", f"{fv}/report/daily", f"{fv}/report/weekly", f"{fv}/restarts",
              f"{fv}/events", f"{fv}/samples", f"{fv}/funnel", f"{fv}/funnel/{DAY}", f"{fv}/frozen-controls", f"{fv}/run", "/api/strategy-registry",
              "/api/strategy-registry/DOWNSIDE_DISLOCATION_RECOVERY"]
             + [f"{fv}/closed/{p}" for p in pids.values()] + [f"{fv}/sessions/{t}" for t in DATES]
             + [f"{fv}/{r}/{t}" for r in ("daily", "report/daily") for t in DATES + ["2026-10-22"]]           # 2026-10-22: no stored row
             + [f"{fv}/export/{k}?format={f}" for k in EXPORTS + CAPTURE_EXPORTS for f in ("csv", "json")])
    templates = {r.path for r in FVR.router.routes if "GET" in r.methods}
    covered = {t for t in templates if any(re.fullmatch(re.sub(r"\{[^}]+\}", "[^/?]+", t) + r"(\?.*)?", p) for p in paths)}
    assert covered == templates, templates - covered                                         # every GET route of the router, none left out
    got = {}
    held = sqlite3.connect(f"file:{d.db.as_posix()}?mode=ro", uri=True)
    try:
        for path in paths:
            rows0, v0 = _db_rows(d.db), held.execute("PRAGMA data_version").fetchone()[0]
            r = c.get(path)
            assert r.status_code == 200, (path, r.status_code, r.text[:200])
            rows1 = _db_rows(d.db)
            assert rows1 == rows0, (path, sorted(t for t in rows0.keys() | rows1.keys() if rows0.get(t) != rows1.get(t)))
            assert held.execute("PRAGMA data_version").fetchone()[0] == v0, path               # no other connection committed anything
            got[path] = r
    finally:
        held.close()
    assert {k: d.xs.position(p).realized_pnl for k, p in pids.items()} == raw                  # the raw values kept ...
    assert d.fvs.daily(DAY) == stored_day and len(d.fvs.events(kind="SUMMARY_REBUILT")) == rebuilt
    for k in ("amb", "zero", "nop"):                                                           # ... while every read serves the unverified trades null
        tr = got[f"{fv}/closed/{pids[k]}"].json()
        assert tr["verified"] is False and all(tr["final_pnl"][f] is None for f in ("realized_pnl", "gross_pnl", "net_pnl", "return_pct")), k
    closed = {t["position_id"]: t for t in got[f"{fv}/closed"].json()["closed"]}
    assert {k: closed[p]["realized_pnl"] for k, p in pids.items() if k != "open"} == {"ok": "10.00", "healed": "4.80", "amb": None, "zero": None, "nop": None}
    day = got[f"{fv}/daily/{DAY}"].json()
    assert (day["pnl_status"], day["unverified"], day["realized_pnl"]) == ("UNVERIFIED_PRESENT", 3, "14.80")
    assert day["pnl_watermark"] != stored_day["pnl_watermark"]                                 # the stored row was stale: a read had something to write
    facts = got["/api/strategy-registry"].json()["facts"]
    assert facts["validation"] is not None and not [e for e in facts["errors"] if e.startswith("forward_validation")]   # overview() ran


def _w_heal(d, ex, prices):
    it = iter(prices)

    def heal():
        th = threading.Thread(target=lambda: d.observe(ex, "filled", 10, next(it)))
        th.start()
        th.join()
    return heal


@pytest.mark.parametrize("heal_on,expect", [({1}, "OK"), ({1, 2}, "OK"), ({1, 2, 3}, "UNSTABLE")])
def test_TA30_a_heal_between_the_watermark_reads_never_yields_a_mixed_or_stale_number(tmp_path, monkeypatch, heal_on, expect):
    d = Direct(tmp_path)
    p = d.closed_trade(d.entry(), "10.00", "10.20")
    ex = d.xs.exit_intents_for_position(p.position_id)[0]
    heal, orig, calls = _w_heal(d, ex, ["10.21", "10.22", "10.23"]), d.fv.daily_summary, []

    def hooked(td, now=None):
        calls.append(td)
        if len(calls) in heal_on:
            heal()                                                                            # inside daily_summary: between the bracket's two W reads
        return orig(td, now)
    monkeypatch.setattr(d.fv, "daily_summary", hooked)
    s = d.fv.bracketed_summary(DAY)
    q = d.xs.position(p.position_id)
    if expect == "OK":
        assert len(calls) == max(heal_on) + 1 and s["pnl_status"] == "OK" and s["realized_pnl"] == q.realized_pnl and s["pnl_watermark"] == d.fv.pnl_watermark(DAY)
    else:
        assert s["pnl_status"] == "UNSTABLE" and s["realized_pnl"] is None and s["win_loss"] is None and "pnl_watermark" not in s
        assert all(t["realized_pnl"] is None and t["return_pct"] is None for t in s["closed_trades"])
    assert d.fvs.daily(DAY) is None and d.fvs.events(kind="SUMMARY_REBUILT") == []          # nothing written by the read


def test_TA31_the_pnl_status_values(tmp_path, monkeypatch):
    d = Direct(tmp_path)
    assert d.fv.bracketed_summary("2026-10-20")["pnl_status"] == "NO_TRADES"
    d.closed_trade(d.entry(xid="a" * 32), "10.00", "11.00")
    assert d.fv.bracketed_summary(DAY)["pnl_status"] == "OK"
    bad = d.entry(xid="b" * 32)
    d.observe(bad, "filled", 10, "10.00")
    d.observe(d.exit(d.pos(bad), "c" * 32, 10), "filled", 10, None)
    s = d.fv.bracketed_summary(DAY)
    assert (s["pnl_status"], s["realized_pnl"], s["unverified"]) == ("UNVERIFIED_PRESENT", "10.00", 1)
    assert [t["realized_pnl"] for t in sorted(s["closed_trades"], key=lambda t: t["verified"])] == [None, "10.00"]
    monkeypatch.setattr(d.xs, "accounting_view", lambda pid: (_ for _ in ()).throw(sqlite3.OperationalError("disk I/O error")))
    s = d.fv.bracketed_summary(DAY)
    assert s["pnl_status"] == "UNSTABLE" and s["realized_pnl"] is None                       # T-A41 below checks the rows


def test_TA32_unconfirmed_ambiguous_or_inconsistent_intents_never_verify_and_a_quantity_decrease_moves_the_watermark(tmp_path):
    d = Direct(tmp_path)
    ent = d.entry(xid="a" * 32)                                                               # (i) an exit never found at the broker
    d.observe(ent, "filled", 10, "10.00")
    x1 = d.exit(d.pos(ent), "b" * 32, 10, cycle=1)
    d.xs.update_intent(x1.execution_id, d.tick(), state="NOT_SENT", failure_code="NOT_FOUND_AFTER_LOOKUPS", resolved_at=d.t.isoformat(timespec="seconds"))
    d.xs.update_position(d.pos(ent).position_id, d.t, state="EXIT_REQUIRED")
    d.observe(d.exit(d.pos(ent), "c" * 32, 10, cycle=2), "filled", 10, "11.00")
    assert "INTENT_NOT_FOUND_AT_BROKER" in reasons(d, d.pos(ent))
    p2 = d.closed_trade(d.entry(xid="d" * 32))                                               # (ii) an ambiguous outcome class
    d.xs.update_intent("d" * 32, d.tick(), outcome_class="O7")
    assert "INTENT_AMBIGUOUS" in reasons(d, d.xs.position(p2.position_id))
    p3 = d.closed_trade(d.entry(xid="e" * 32))                                               # (iii) sum(Q_o) != exit_qty, P/L not NULL
    with sqlite3.connect(d.db) as c:
        c.execute("UPDATE execution_intents SET filled_qty = 9 WHERE position_id = ? AND intent_type = 'EXIT'", (p3.position_id,))
    assert "EXIT_QTY_SUM_MISMATCH" in reasons(d, d.xs.position(p3.position_id))
    p4 = d.closed_trade(d.entry(xid="f" * 32))                                               # (iv) a quantity decrease moves W(d)
    w0 = d.fv.pnl_watermark(DAY)
    ex = d.xs.exit_intents_for_position(p4.position_id)[0]
    d.xs.record_order(order(d.oids[ex.execution_id], ex.client_order_id, "SNDK", "sell", 10, "filled", 8, "11.00"), ex.execution_id, d.tick(), "monitor")
    assert d.fv.pnl_watermark(DAY) != w0


def test_TA33_a_verification_failure_never_raises_into_capture_and_the_capture_outcome_is_unchanged(tmp_path, monkeypatch):
    d = Direct(tmp_path)
    p = d.closed_trade(d.entry(), "10.00", "11.00")
    monkeypatch.setattr(d.xs, "accounting_view", lambda pid: (_ for _ in ()).throw(RuntimeError("injected")))
    t = d.fv._trade(p)
    assert (t["verified"], t["unverified_reasons"]) == (False, ["VERIFY_ERROR"]) and t["gross_pnl"] == "10.00"
    out = d.fv.capture._outcome(p, "LEGACY", "LEGACY", "strategy_v")
    base = {"version", "position_id", "execution_id", "setup_id", "symbol", "cohort_id", "cross_cohort_hold", "strategy_version", "controls_version", "contract_hash", "entry_at",
            "exit_at", "holding_seconds", "holding_sessions", "entry_qty", "exit_qty", "average_entry", "average_exit", "realized_pnl", "gross_pnl", "return_pct", "exit_reason",
            "exit_cycles", "entry_slippage", "exit_slippage", "excursions", "fees_note"}
    assert set(out) == base and (out["gross_pnl"], out["return_pct"], out["realized_pnl"]) == ("10.00", "10.00", "10.00")


def test_TA34_no_early_positive_while_a_trade_is_unverified(tmp_path):
    d = Direct(tmp_path)
    days = [(T0 - timedelta(days=k)).date().isoformat() for k in range(1, 60) if (T0 - timedelta(days=k)).weekday() < 5][:30]
    sessions(d, days)
    for n in range(30):
        d.closed_trade(d.entry(xid=f"{n:032d}"), "10.00", "11.00", xid=f"x{n:031d}")
    st = d.fv.cumulative()["strategy"]
    from forward_validation import metrics as M                                                # Wave 2 PR C (C-35): the all-time label is informational; the rule is the run block's
    assert st["edge_status"] == "INSUFFICIENT_DATA" and st["unverified"] == 0 and st["expectancy_status"]["reasons"][0].startswith("not a forward run")
    assert M.edge_status(st["closed_trades"], 30, st["expectancy"], st["win_rate"], "0.00", unverified=st["unverified"])["status"] == "EARLY_POSITIVE"   # the control: the rule can say positive
    bad = d.entry(xid="b" * 32)
    d.observe(bad, "filled", 10, "10.00")
    d.observe(d.exit(d.pos(bad), "c" * 32, 10), "filled", 10, None)
    st = d.fv.cumulative()["strategy"]
    assert st["edge_status"] == "INSUFFICIENT_DATA" and st["unverified"] == 1
    assert "unverified trade" in M.edge_status(st["closed_trades"], 30, st["expectancy"], st["win_rate"], "0.00", unverified=st["unverified"])["reasons"][0]   # the cap (C-53)


def test_TA35_a_new_process_persists_each_stale_date_once_at_most_three_per_tick(tmp_path):
    d = Direct(tmp_path)
    sessions(d, DATES)
    for t in DATES:
        d.fvs.put_daily(t, d.t, FV_VERSION, d.fv.daily_summary(t))
    fresh = ForwardValidationService(stream_fn=lambda: SimpleNamespace(execution=d.x), store=d.fvs, clock=lambda: d.t)    # a restarted observer: token None
    o1 = fresh._persist_stale_summaries(d.tick())
    o2 = fresh._persist_stale_summaries(d.tick())
    o3 = fresh._persist_stale_summaries(d.tick())
    assert (o1["refreshed"] + o1["rebuilt"], o2["refreshed"] + o2["rebuilt"], o3["checked"]) == (3, 2, 0)
    stamps = {t: d.fvs.daily(t)["computed_at"] for t in DATES}
    again = ForwardValidationService(stream_fn=lambda: SimpleNamespace(execution=d.x), store=d.fvs, clock=lambda: d.t)
    o4 = again._persist_stale_summaries(d.tick())
    assert o4["checked"] == 0 and {t: d.fvs.daily(t)["computed_at"] for t in DATES} == stamps     # a current date is never rewritten


# ================================================================ T-A36 ... T-A45: fail-closed P/L on every surface (Revision 3.2) ==========

def _three_trades(d):
    """A verified trade, an unverified one WITH prices (an ambiguous entry), and an ENTRY_PRICE_INVALID one with sampled marks (J-02)."""
    ok = d.closed_trade(d.entry(xid="a" * 32), "10.00", "11.00")
    amb = d.closed_trade(d.entry(xid="b" * 32), "10.00", "12.00")
    d.xs.update_intent("b" * 32, d.tick(), outcome_class="O8")
    zero = d.xs.insert_intent(Intent(execution_id="c" * 32, intent_type="ENTRY", symbol="SNDK", side="buy", qty=10, client_order_id="stockagent-entry-" + "c" * 32,
                                     created_at=S.TS, updated_at=S.TS, attempt_count=1, submitted_at=S.TS, state="SUBMITTED", invalidation_price="9.00", reference_price="10.00"))
    d.observe(zero, "filled", 10, None)
    zp = d.pos(zero)
    d.fvs.add_marks([{"at": d.t.isoformat(timespec="seconds"), "position_id": zp.position_id, "symbol": "SNDK", "state": "OPEN", "quantity": 10, "last_price": "11.00",
                      "last_price_at": d.t.isoformat(timespec="seconds")}])
    d.observe(d.exit(zp, "d" * 32, 10), "filled", 10, "11.00")
    return d.xs.position(ok.position_id), d.xs.position(amb.position_id), d.pos(zero)


def test_TA36_closed_and_the_trace_null_an_unverified_trades_pnl_and_keep_the_raw_values_as_labelled_evidence(tmp_path, monkeypatch):
    d = Direct(tmp_path)
    ok, amb, zero = _three_trades(d)
    c = api(monkeypatch, d)
    rows = {t["position_id"]: t for t in c.get("/api/forward-validation/closed").json()["closed"]}
    t = rows[ok.position_id]
    assert (t["verified"], t["unverified_reasons"], t["realized_pnl"], t["gross_pnl"], t["return_pct"]) == (True, [], "10.00", "10.00", "10.00")
    assert t["recorded_pnl"]["gross_pnl"] == "10.00"
    u = rows[amb.position_id]
    assert u["verified"] is False and u["unverified_reasons"] == ["INTENT_AMBIGUOUS"]
    assert (u["realized_pnl"], u["gross_pnl"], u["net_pnl"], u["return_pct"]) == (None, None, None, None)            # never 0
    assert (u["recorded_pnl"]["realized_pnl"], u["recorded_pnl"]["gross_pnl"], u["recorded_pnl"]["return_pct"]) == ("20.00", "20.00", "20.00")
    assert "not a verified result" in u["recorded_pnl"]["basis"] and (u["entry_price"], u["exit_price"], u["qty"]) == ("10.00", "12.00", 10)
    z = rows[zero.position_id]
    assert "ENTRY_PRICE_INVALID" in z["unverified_reasons"] and all(z["excursions"][k] is None for k in ("mfe_pnl", "mae_pnl", "mfe_pct", "mae_pct"))   # J-02
    assert (z["entry_slippage"]["slippage_absolute"], z["entry_slippage"]["slippage_bps"]) == (None, None)
    assert z["recorded_pnl"]["excursions"]["mfe_pnl"] is not None and z["recorded_pnl"]["entry_slippage"]["slippage_bps"] == "-10000.00"
    tr = c.get(f"/api/forward-validation/closed/{amb.position_id}").json()
    assert tr["verified"] is False and tr["final_pnl"]["verified"] is False and all(tr["final_pnl"][k] is None for k in ("realized_pnl", "gross_pnl", "net_pnl", "return_pct"))
    assert tr["recorded_pnl"]["realized_pnl"] == "20.00" and tr["final_pnl"]["notional"] == "100.00"
    tv = c.get(f"/api/forward-validation/closed/{ok.position_id}").json()
    assert tv["final_pnl"]["realized_pnl"] == "10.00" and tv["final_pnl"]["gross_pnl"] == "10.00" and tv["final_pnl"]["verified"] is True


def _base_tuple(name):
    tree = ast.parse(at_base("forward_validation/reports.py"))
    node = next(n for n in tree.body if isinstance(n, ast.Assign) and n.targets[0].id == name)
    return tuple(ast.literal_eval(node.value))


def test_TA37_the_trades_export_appends_verified_and_leaves_unverified_pnl_cells_empty(tmp_path, monkeypatch):
    d = Direct(tmp_path)
    ok, amb, zero = _three_trades(d)
    c = api(monkeypatch, d)
    txt = c.get("/api/forward-validation/export/trades").text
    import csv as _csv
    import io as _io
    rd = list(_csv.DictReader(_io.StringIO(txt)))
    header = txt.splitlines()[0].split(",")
    assert tuple(header) == _base_tuple("TRADE_COLS") + ("verified", "run_id", "member", "exclusion_reason", "time_source") and header[0] == "position_id"   # Wave 2 PR C: the run columns
    rows = {r["position_id"]: r for r in rd}
    assert (rows[ok.position_id]["realized_pnl"], rows[ok.position_id]["gross_pnl"], rows[ok.position_id]["return_pct"], rows[ok.position_id]["verified"]) == ("10.00", "10.00", "10.00", "True")
    assert (rows[amb.position_id]["realized_pnl"], rows[amb.position_id]["gross_pnl"], rows[amb.position_id]["return_pct"], rows[amb.position_id]["verified"]) == ("", "", "", "False")
    assert (rows[zero.position_id]["mfe_pnl"], rows[zero.position_id]["entry_slippage_bps"]) == ("", "")
    j = json.loads(c.get("/api/forward-validation/export/trades?format=json").text)
    assert j["columns"][-5:] == ["verified", "run_id", "member", "exclusion_reason", "time_source"] and {r["position_id"]: r["gross_pnl"] for r in j["rows"]}[amb.position_id] is None   # Wave 2 PR C


def test_TA38_the_daily_export_appends_the_pnl_status_and_never_writes_a_zero_for_a_null(tmp_path, monkeypatch):
    d = Direct(tmp_path)
    sessions(d, ["2026-10-19", "2026-10-20", DAY])
    d.t = T0 - timedelta(days=2)                                                               # 2026-10-19: OK
    d.closed_trade(d.entry(xid="a" * 32), "10.00", "11.00")
    d.t = T0 - timedelta(days=1)                                                               # 2026-10-20: UNVERIFIED_PRESENT with no verified trade (J-03)
    z = d.entry(xid="b" * 32)
    d.observe(z, "filled", 10, "10.00")
    d.observe(d.exit(d.pos(z), "c" * 32, 10), "filled", 10, None)
    d.t = T0                                                                                   # DAY: forced UNSTABLE
    d.closed_trade(d.entry(xid="d" * 32), "10.00", "11.00")
    for t in ("2026-10-19", "2026-10-20", DAY):
        d.fvs.put_daily(t, d.t, FV_VERSION, d.fv.daily_summary(t))
    real = d.fv.pnl_watermark
    monkeypatch.setattr(d.fv, "pnl_watermark", lambda td, rows=None: (_ for _ in ()).throw(OSError("busy")) if td == DAY and rows is None else real(td, rows))
    c = api(monkeypatch, d)
    txt = c.get("/api/forward-validation/export/daily").text
    header = txt.splitlines()[0].split(",")
    assert tuple(header) == _base_tuple("DAILY_COLS") + ("pnl_status", "unverified", "run_id", "member", "exclusion_reason", "time_source")   # Wave 2 PR C: the run columns
    import csv as _csv
    import io as _io
    rows = {r["trading_date"]: r for r in _csv.DictReader(_io.StringIO(txt))}
    assert (rows["2026-10-19"]["pnl_status"], rows["2026-10-19"]["realized_pnl"], rows["2026-10-19"]["unverified"]) == ("OK", "10.00", "0")
    assert (rows["2026-10-20"]["pnl_status"], rows["2026-10-20"]["realized_pnl"], rows["2026-10-20"]["unverified"]) == ("UNVERIFIED_PRESENT", "", "1")
    assert (rows[DAY]["pnl_status"], rows[DAY]["realized_pnl"]) == ("UNSTABLE", "")


def _summaries(d, monkeypatch):
    sessions(d, ["2026-10-16", "2026-10-19", "2026-10-20", DAY])
    out = {"NO_TRADES": d.fv.bracketed_summary("2026-10-16")}
    d.t = T0 - timedelta(days=2)
    d.closed_trade(d.entry(xid="a" * 32), "10.00", "11.00")
    out["OK"] = d.fv.bracketed_summary("2026-10-19")
    d.t = T0 - timedelta(days=1)
    z = d.entry(xid="b" * 32)
    d.observe(z, "filled", 10, "10.00")
    d.observe(d.exit(d.pos(z), "c" * 32, 10), "filled", 10, None)
    out["ZERO_VERIFIED"] = d.fv.bracketed_summary("2026-10-20")
    d.t = T0
    d.closed_trade(d.entry(xid="d" * 32), "10.00", "10.50")
    y = d.entry(xid="e" * 32)
    d.observe(y, "filled", 10, "10.00")
    d.observe(d.exit(d.pos(y), "f" * 32, 10), "filled", 10, None)
    out["PARTIAL"] = d.fv.bracketed_summary(DAY)
    out["UNSTABLE"] = d.fv._null_pnl(out["PARTIAL"])
    out["NO_STATUS"] = {k: v for k, v in out["OK"].items() if k != "pnl_status"}
    return out


def test_TA39_the_daily_report_labels_partial_unstable_and_unknown_pnl(tmp_path, monkeypatch):
    d = Direct(tmp_path)
    s = _summaries(d, monkeypatch)
    rep = {k: REP.daily_report(v) for k, v in s.items()}

    def line(k, label):
        return next(ln for ln in rep[k].splitlines() if ln.strip().startswith(label))
    assert all("P/L status" in r for r in rep.values())
    assert line("OK", "Realized paper P/L").split()[-1] == "10.00" and "(partial" not in rep["OK"]
    assert line("NO_TRADES", "Realized paper P/L").endswith("not available")
    assert line("PARTIAL", "Realized paper P/L").endswith("5.00 (partial: verified trades only; 1 unverified)") and "(verified trades only)" in line("PARTIAL", "Closed trades")
    assert "P/L unverified" in rep["PARTIAL"] and "P/L 5.00" in rep["PARTIAL"]
    assert line("ZERO_VERIFIED", "Realized paper P/L").endswith("not available (no verified trades; 1 unverified)")            # J-03
    assert line("UNSTABLE", "Realized paper P/L").endswith("not available (accounting changing or incomplete during the read)")
    assert line("NO_STATUS", "Realized paper P/L").endswith("not available (no P/L status)") and "10.00" not in line("NO_STATUS", "Realized paper P/L")


def test_TA40_the_weekly_report_never_sums_an_unstable_or_unknown_day_silently(tmp_path):
    def day(i, status, realized, unv=0, n=1):
        return {"trading_date": f"2026-10-{19 + i}", "session_status": "HEALTHY", "pnl_status": status, "realized_pnl": realized, "unverified": unv, "win_loss": {"closed_trades": n}}
    cum0 = {"as_of": "x", "strategy": {"closed_trades": 5, "win_rate": "60.00", "average_win": "2.00", "average_loss": "1.00", "expectancy": "0.80", "unverified": 0},
            "run": {"defined": True, "sessions": {"member_dates": [f"2026-10-{19 + i}" for i in range(5)]}}}   # Wave 2 PR C (C-88): the five days are run members
    cum1 = {**cum0, "strategy": {**cum0["strategy"], "unverified": 1}}
    week = [day(i, "OK", "1.00") for i in range(5)]

    def pnl_line(rep):
        return next(ln for ln in rep.splitlines() if "Realized paper P/L (week)" in ln)
    assert pnl_line(REP.weekly_report(cum0, week)).endswith("5.00")
    assert pnl_line(REP.weekly_report(cum0, week[:4] + [day(4, "UNVERIFIED_PRESENT", "1.00", 2)])).endswith("5.00 (partial: verified trades only; 2 unverified)")
    assert pnl_line(REP.weekly_report(cum0, week[:4] + [day(4, "UNSTABLE", None)])).endswith("not available (P/L unstable or unknown on 2026-10-23)")
    assert pnl_line(REP.weekly_report(cum0, week[:4] + [{k: v for k, v in day(4, None, "1.00").items() if k != "pnl_status"}])).endswith("not available (P/L unstable or unknown on 2026-10-23)")
    zero = [day(i, "NO_TRADES", None, n=0) for i in range(4)] + [day(4, "UNVERIFIED_PRESENT", None, 1, n=0)]
    assert pnl_line(REP.weekly_report(cum0, zero)).endswith("not available (no verified trades; 1 unverified)")             # J-03: never 0.00
    r1 = REP.weekly_report(cum1, week)
    import re as _re
    assert _re.search(r"Closed trades overall\s+5 \(verified trades only; 1 unverified\)", r1) and "(verified trades only; 1 unverified)" not in REP.weekly_report(cum0, week)


def test_TA41_an_unstable_summary_nulls_every_per_trade_pnl_and_keeps_verified(tmp_path, monkeypatch):
    d = Direct(tmp_path)
    d.closed_trade(d.entry(xid="a" * 32), "10.00", "11.00")
    monkeypatch.setattr(d.fv, "pnl_watermark", lambda td, rows=None: (_ for _ in ()).throw(OSError("busy")))
    s = d.fv.bracketed_summary(DAY)
    assert s["pnl_status"] == "UNSTABLE" and s["realized_pnl"] is None and s["win_loss"] is None
    assert [(t["verified"], t["realized_pnl"], t["return_pct"]) for t in s["closed_trades"]] == [(True, None, None)]


def test_TA42_downstream_compatibility_capture_keeps_raw_values_and_the_known_consumers_are_unchanged(tmp_path):
    d = Direct(tmp_path)
    ok, amb, zero = _three_trades(d)
    out = d.fv.capture._outcome(amb, "LEGACY", "LEGACY", "strategy_v")
    raw = M.trade_pnl(amb.average_entry_price, amb.average_exit_price, amb.exit_qty)
    assert (out["gross_pnl"], out["return_pct"], out["realized_pnl"]) == (raw["gross_pnl"], raw["return_pct"], amb.realized_pnl) == ("20.00", "20.00", "20.00")   # (i)

    def fn_src(text, name):
        tree = ast.parse(text)
        return ast.dump(next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name.startswith(name)))
    now61 = FL.undo_w2b("tests/test_forward_validation_61.py", FL.undo_w2c("tests/test_forward_validation_61.py", FL.undo_ex06("tests/test_forward_validation_61.py", FL.undo_panel("tests/test_forward_validation_61.py", (ROOT / "tests" / "test_forward_validation_61.py").read_text(encoding="utf-8")))))   # Wave 2 PR B (D-1) / PR C (T-WC06): PR A's delta; their own edits: F02B / F03B, F02C / F03C
    base61 = at_base("tests/test_forward_validation_61.py")
    for name in ("test_T01_", "test_T02_", "test_R01_", "test_R02_", "test_S01_", "test_X01_"):         # (ii) they run in this suite, unchanged
        assert fn_src(now61, name) == fn_src(base61, name), name
    now610a, base610a = FL.undo_w2b("tests/test_forward_paper_610a.py", FL.undo_w2c("tests/test_forward_paper_610a.py", FL.undo_ex06("tests/test_forward_paper_610a.py", FL.undo_xss01("tests/test_forward_paper_610a.py", FL.undo_panel("tests/test_forward_paper_610a.py", (ROOT / "tests" / "test_forward_paper_610a.py").read_text(encoding="utf-8")))))), at_base("tests/test_forward_paper_610a.py")
    tests610a = [n.name for n in ast.parse(base610a).body if isinstance(n, ast.FunctionDef) and n.name.startswith("test_") and not n.name.startswith("test_F")]
    assert len(tests610a) > 20 and all(fn_src(now610a, n) == fn_src(base610a, n) for n in tests610a)   # only its F (freeze bookkeeping) tests carry the PR A union
    for rel in ("tests/frontend/forward_validation.test.cjs", "frontend/forward_validation.js"):
        assert FL.undo_w2c(rel, FL.undo_ex06(rel, FL.undo_xss01(rel, FL.undo_panel(rel, FL.pre_w2c(rel, FL.pre_ex06(rel, (ROOT / rel).read_text(encoding="utf-8"))))))) == at_base(rel), rel   # Wave 2 PR C (T-WC06): its W2-7 edits are its own pins
    flows = (ROOT / "browser_tests" / "flows.py").read_text(encoding="utf-8").replace("\r\n", "\n").splitlines()
    assert flows[2569:2590] == at_base("browser_tests/flows.py").splitlines()[2569:2590]


def test_TA43_an_incomplete_or_unreadable_summary_is_unstable_and_never_persisted(tmp_path, monkeypatch):
    d = Direct(tmp_path)
    d.closed_trade(d.entry(xid="a" * 32), "10.00", "11.00")
    d.closed_trade(d.entry(xid="b" * 32), "10.00", "11.00")
    sessions(d, [DAY])
    d.fvs.put_daily(DAY, d.t, FV_VERSION, d.fv.daily_summary(DAY))
    real = d.fv.closed_trades
    monkeypatch.setattr(d.fv, "closed_trades", lambda limit=500, trading_date=None: [])            # RO4: a swallowed read error
    s = d.fv.bracketed_summary(DAY)
    assert s["pnl_status"] == "UNSTABLE" and s["realized_pnl"] is None
    monkeypatch.setattr(d.fv, "closed_trades", lambda limit=500, trading_date=None: real(limit, trading_date)[:1])   # the limit x 3 cut
    assert d.fv.bracketed_summary(DAY)["pnl_status"] == "UNSTABLE"
    out = d.fv._persist_stale_summaries(d.tick())
    assert out["rebuilt"] + out["refreshed"] == 0 and out["pending"] is True and "pnl_watermark" not in d.fvs.daily(DAY)
    monkeypatch.setattr(d.fv, "closed_trades", real)
    with pytest.raises(sqlite3.Error):
        ExecutionStore(tmp_path / "absent.db").pnl_watermark_rows("a", "z")                    # raises, never []
    (tmp_path / "empty.db").write_bytes(b"")
    with pytest.raises(sqlite3.Error):
        ExecutionStore(tmp_path / "empty.db").pnl_watermark_rows("a", "z")
    with pytest.raises(sqlite3.Error):
        ExecutionStore(tmp_path / "empty.db").accounting_view("x")                              # J-08
    monkeypatch.setattr(d.x, "store", ExecutionStore(tmp_path / "empty.db"))
    s = d.fv.bracketed_summary(DAY)
    assert s["pnl_status"] == "UNSTABLE"
    monkeypatch.setattr(d.x, "store", d.xs)                                                  # K-06: W readable, the verification read fails
    monkeypatch.setattr(d.xs, "accounting_view", lambda pid: (_ for _ in ()).throw(sqlite3.OperationalError("disk I/O error")))
    puts, real_put = [], d.fvs.put_daily
    monkeypatch.setattr(d.fvs, "put_daily", lambda *a, **k: (puts.append(a[0]), real_put(*a, **k)))
    out = d.fv._persist_stale_summaries(d.tick())
    assert puts == [] and out["pending"] is True and "pnl_watermark" not in d.fvs.daily(DAY)        # a VERIFY_ERROR summary is never persisted


def test_TA44_a_capture_refresh_rewrite_is_never_served_and_is_replaced_in_the_same_tick(tmp_path, monkeypatch):
    from forward_capture.health import _refresh_daily
    d = Direct(tmp_path)
    ent, heal = healed_trade(d)
    _five_days_current(d)
    heal()
    _refresh_daily(SimpleNamespace(fv=d.fv), DAY, d.tick())                                    # 6.10A: an unbracketed daily_summary over the stored row
    row = d.fvs.daily(DAY)
    assert "pnl_watermark" not in row and row["realized_pnl"] == "4.80"
    c = api(monkeypatch, d)
    served = c.get(f"/api/forward-validation/daily/{DAY}").json()
    assert served.get("pnl_watermark") == d.fv.pnl_watermark(DAY)                              # the fresh bracketed summary, never the unchecked row
    assert c.get(f"/api/forward-validation/sessions/{DAY}").json()["daily"].get("pnl_watermark") == d.fv.pnl_watermark(DAY)
    d.fv._persist_stale_summaries(d.tick())
    assert d.fvs.daily(DAY)["pnl_watermark"] == d.fv.pnl_watermark(DAY)


def test_TA44b_an_unbracketed_rewrite_with_no_executor_write_is_still_persisted_on_the_next_tick(tmp_path):
    """Checkpoint 1 K-05: the `unmarked` scan alone (the token unchanged) brings a capture rewrite back to a watermarked row."""
    from forward_capture.health import _refresh_daily
    d = Direct(tmp_path)
    d.closed_trade(d.entry(), "10.00", "11.00")
    _five_days_current(d)
    d.fv._persist_stale_summaries(d.tick())                                                   # quiet: every row current, the token taken
    assert d.fv._persist_stale_summaries(d.tick())["checked"] == 0
    _refresh_daily(SimpleNamespace(fv=d.fv), DAY, d.tick())                                  # no executor write: the token does not move
    assert "pnl_watermark" not in d.fvs.daily(DAY)
    out = d.fv._persist_stale_summaries(d.tick())
    assert out["checked"] == 1 and d.fvs.daily(DAY)["pnl_watermark"] == d.fv.pnl_watermark(DAY)


def test_TA45_a_watermark_move_without_a_pnl_change_writes_no_rebuilt_event(tmp_path, monkeypatch):
    d = Direct(tmp_path)
    p = d.closed_trade(d.entry(xid="a" * 32), "10.00", "11.00")
    sessions(d, ["2026-10-20", DAY])
    legacy = {k: v for k, v in d.fv.daily_summary("2026-10-20").items() if k not in ("pnl_status", "unverified")}
    d.fvs.put_daily("2026-10-20", d.t, FV_VERSION, legacy)                                     # J-09: a legacy no-trade row
    d.fvs.put_daily(DAY, d.t, FV_VERSION, d.fv.daily_summary(DAY))
    d.fv._persist_stale_summaries(d.tick())
    assert d.fvs.events(kind="SUMMARY_REBUILT") == []                                          # session-close content and the legacy no-trade day: equal
    now = d.tick().isoformat(timespec="seconds")                                               # an intent is always written at the executor's current time
    ghost = d.xs.insert_intent(Intent(execution_id="g" * 32, intent_type="EXIT", symbol="SNDK", side="sell", qty=1, client_order_id="stockagent-exit-ghost", created_at=now,
                                      updated_at=now, attempt_count=1, submitted_at=now, state="SUBMITTED", position_id=p.position_id, exit_cycle=9))
    d.fv._persist_stale_summaries(d.tick())
    assert len(d.fvs.events(kind="SUMMARY_REBUILT")) == 1                                      # the open intent un-verifies the trade: a real change
    puts, real_put = [], d.fvs.put_daily
    monkeypatch.setattr(d.fvs, "put_daily", lambda *a, **k: (puts.append(a[0]), real_put(*a, **k)))
    for i in range(5):                                                                         # the ghost's broker view flaps: W moves, the P/L does not
        d.xs.record_order(order(S.OID(900), "stockagent-exit-ghost", "SNDK", "sell", 1, "new" if i % 2 else "accepted", 0, None), ghost.execution_id, d.tick(), "monitor")
        n = len(puts)
        d.fv._persist_stale_summaries(d.tick())
        assert len(puts) - n <= 1
    assert len(d.fvs.events(kind="SUMMARY_REBUILT")) == 1 and d.fvs.daily(DAY)["pnl_watermark"] == d.fv.pnl_watermark(DAY)
    d.xs.update_intent(ghost.execution_id, d.tick(), state="CANCELED", resolved_at=d.t.isoformat(timespec="seconds"))
    d.fv._persist_stale_summaries(d.tick())
    assert len(d.fvs.events(kind="SUMMARY_REBUILT")) == 2                                      # verified again: exactly one more


# ================================================================ the offline checker (tools/verify_exit_fills.py) ===========================

def test_TV01_the_offline_checker_reports_a_pf09_row_reads_only_and_matches_the_executor_formula(tmp_path, capsys):
    from execution.service import recompute_pnl
    from tools import verify_exit_fills as V
    d = Direct(tmp_path)
    ent = d.entry()
    d.observe(ent, "partially_filled", 4, "10.00")
    d.observe(ent, "filled", 10, "10.30")
    ex = d.exit(d.pos(ent), "x" * 32, 10)
    d.observe(ex, "partially_filled", 4, "12.00")
    d.observe(ex, "filled", 10, "10.20")
    d.closed_trade(d.entry(xid="b" * 32), "10.00", "11.00")
    with sqlite3.connect(d.db) as c:
        c.execute("UPDATE paper_positions SET average_exit_price = '10.92', realized_pnl = '6.20' WHERE position_id = ?", (d.pos(ent).position_id,))   # as 5d6f7dd wrote it
    h0 = content_hash(d.db)
    out = V.check(d.db)
    assert content_hash(d.db) == h0 and out["counts"] == {"MATCH": 1, "DIFFERS": 1, "CANNOT_REDERIVE": 0}
    bad = next(r for r in out["positions"] if r["verdict"] == "DIFFERS")
    assert (bad["stored_realized_pnl"], bad["rederived_realized_pnl"], bad["difference"]) == ("6.20", "-1.00", "-7.20")
    assert V.main(["--db", str(d.db), "--json"]) == 1 and json.loads(capsys.readouterr().out)["counts"]["DIFFERS"] == 1
    rnd = random.Random(7)
    for _ in range(300):
        exits = [(rnd.randint(0, 6), rnd.choice([None, "0", f"{rnd.randint(500, 1500) / 100:.2f}"])) for _ in range(rnd.randint(0, 3))]
        q = sum(e[0] for e in exits) + rnd.choice([0, 0, 1])
        e = rnd.choice(["0.00", "10.00", "9.87", None])
        r = recompute_pnl(10, e, q, exits)
        assert V.rederive(e, q, exits) == (r["average_exit_price"], r["realized_pnl"], r["pnl_snapshot"]["valid"])
    with pytest.raises(FileNotFoundError):
        V.check(tmp_path / "nope.db")


def test_TV02_the_checker_mirrors_the_price_retention_so_it_agrees_with_wave_2(tmp_path):
    """Checkpoint 2 L-05: after a price-less repeat at an unchanged quantity (F-12), the latest order event has no price; the checker keeps that
    quantity's latest price as the executor does, so it reports MATCH on the trade Wave 2 verifies."""
    from tools import verify_exit_fills as V
    d = Direct(tmp_path)
    p = d.closed_trade(d.entry(), "10.00", "11.00")
    ex = d.xs.exit_intents_for_position(p.position_id)[0]
    d.observe(ex, "filled", 10, None)                                                          # the broker repeats the fill without its price
    assert d.xs.order_events(d.oids[ex.execution_id])[-1]["filled_avg_price"] is None and d.xs.intent(ex.execution_id).filled_avg_price == "11.00"
    assert d.fv.verification(d.xs.position(p.position_id))["verified"] is True
    out = V.check(d.db)
    assert out["counts"] == {"MATCH": 1, "DIFFERS": 0, "CANNOT_REDERIVE": 0} and "beside Wave 2" in out["note"]
