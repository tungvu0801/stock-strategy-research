"""
tests/wave2_pra_support.py - Wave 2 PR A test support (not a test file): the direct executor rig, synthetic broker order views, a forward
validation service on the same database, the decision-field traces the goldens are taken from, and the whole-database content hash.

Simulation only: tmp_path SQLite, the real ExecutionService / ExecutionStore / ForwardValidationService, broker order views built by hand (the
broker's CUMULATIVE filled_qty / filled_avg_price, as Alpaca reports them). No network, no credential, no real order.
"""
from __future__ import annotations

import hashlib
import json
import random
import sqlite3
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List, Optional, Tuple

from execution.models import Intent
from execution.service import ExecutionService
from execution.store import ExecutionStore
from forward_validation.service import ForwardValidationService
from forward_validation.store import ForwardValidationStore

UTC = timezone.utc
T0 = datetime(2026, 10, 21, 14, 0, tzinfo=UTC)                       # Wednesday 10:00 New York
DAY = "2026-10-21"
TS = T0.isoformat(timespec="seconds")
OPEN = ("PENDING", "SUBMITTED", "PARTIALLY_FILLED", "RECONCILIATION_REQUIRED")
DEC_POS = ("quantity", "entry_qty", "exit_qty", "state", "exit_cycle", "exit_reason", "attention_reason", "closed_at")   # updated_at moves on any P/L write: not compared
DEC_INT = ("state", "filled_qty", "broker_status", "failure_code", "resolved_at")


class FB:
    label, simulated = "ALPACA_PAPER", False


def OID(n: int) -> str:
    return f"bbbbbbbb-0000-4000-8000-{n:012d}"


def order(oid: str, coid: str, sym: str, side: str, qty: int, status: str, fq, px) -> dict:
    return {"id": oid, "client_order_id": coid, "symbol": sym, "side": side, "qty": str(qty), "filled_qty": fq, "filled_avg_price": px, "status": status, "raw": {}}


def cents(x: Decimal) -> str:
    return str(x.quantize(Decimal("0.01")))


class Direct:
    """The executor's fill path driven directly (ExecutionService._apply_order) and a forward-validation service reading the same file."""

    def __init__(self, tmp_path: Path, name: str = "w2.db"):
        tmp_path.mkdir(parents=True, exist_ok=True)
        self.db = tmp_path / name
        self.t = T0
        self.xs = ExecutionStore(self.db)
        self.xs.ensure()
        self.x = ExecutionService(lambda: None, self.xs, broker=FB(), clock=lambda: self.t)
        self.fvs = ForwardValidationStore(self.db)
        self.fvs.ensure()
        self.fv = ForwardValidationService(stream_fn=lambda: SimpleNamespace(execution=self.x), store=self.fvs, clock=lambda: self.t, owner="test-owner")
        self.oids: Dict[str, str] = {}

    def tick(self, minutes: float = 1) -> datetime:
        self.t += timedelta(minutes=minutes)
        return self.t

    def restart(self) -> None:
        """A process restart: a new executor on the same file (nothing in memory survives)."""
        self.x = ExecutionService(lambda: None, ExecutionStore(self.db), broker=FB(), clock=lambda: self.t)
        self.xs = self.x.store

    def entry(self, xid: str = "e" * 32, qty: int = 10, sym: str = "SNDK") -> Intent:
        return self.xs.insert_intent(Intent(execution_id=xid, intent_type="ENTRY", symbol=sym, side="buy", qty=qty, client_order_id="stockagent-entry-" + xid, created_at=TS,
                                            updated_at=TS, attempt_count=1, submitted_at=TS, state="SUBMITTED", invalidation_price="9.00"))

    def exit(self, pos, xid: str, qty: int, cycle: int = 1) -> Intent:
        self.xs.update_position(pos.position_id, self.t, state="EXIT_SUBMITTED", exit_reason="HARD_INVALIDATION", exit_cycle=cycle)
        return self.xs.insert_intent(Intent(execution_id=xid, intent_type="EXIT", symbol=pos.symbol, side="sell", qty=qty, client_order_id=f"stockagent-exit-{pos.position_id}-{cycle}",
                                            created_at=TS, updated_at=TS, attempt_count=1, submitted_at=TS, state="SUBMITTED", position_id=pos.position_id, exit_cycle=cycle,
                                            exit_reason="HARD_INVALIDATION", reference_price="10.00"))

    def observe(self, it: Intent, status: str, fq: int, px: Optional[str], minutes: float = 1) -> None:
        """One broker observation of the intent's order (cumulative quantity and average), applied as the executor's monitor applies it."""
        oid = self.oids.setdefault(it.execution_id, OID(len(self.oids) + 1))
        side = "buy" if it.intent_type == "ENTRY" else "sell"
        self.x._apply_order(self.xs.intent(it.execution_id), order(oid, it.client_order_id, it.symbol, side, it.qty, status, fq, px), self.tick(minutes), "monitor")

    def pos(self, ent: Intent):
        return self.xs.position_for_execution(ent.execution_id)

    def closed_trade(self, ent: Intent, entry_px: str = "10.00", exit_px: str = "11.00", qty: int = 10, xid: Optional[str] = None) -> Any:
        """Entry filled at entry_px, one exit order filled at exit_px: a verified closed trade."""
        self.observe(ent, "filled", qty, entry_px)
        ex = self.exit(self.pos(ent), xid or ("x" + ent.execution_id[1:]), qty)
        self.observe(ex, "filled", qty, exit_px)
        return self.pos(ent)


def content_hash(db: Path) -> Tuple[str, int]:
    """Every table's rows (ordered by all columns, so WITHOUT ROWID tables work), sqlite_master included, and PRAGMA data_version on the same
    connection - the evidence that a read wrote nothing (T-A29)."""
    conn = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True)
    try:
        h = hashlib.sha256()
        for (sql,) in conn.execute("SELECT sql FROM sqlite_master ORDER BY type, name, sql"):
            h.update(str(sql).encode())
        for (name,) in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name").fetchall():
            cols = [r[1] for r in conn.execute(f"PRAGMA table_info('{name}')")]
            for row in conn.execute(f"SELECT * FROM \"{name}\" ORDER BY {', '.join(str(i + 1) for i in range(len(cols)))}"):
                h.update(repr((name, row)).encode())
        return h.hexdigest(), conn.execute("PRAGMA data_version").fetchone()[0]
    finally:
        conn.close()


# ---------------------------------------------------------------- decision-field traces (T-A28; the golden is taken on 5d6f7dd) --------------
def _trace_obs(seed: int) -> List[tuple]:
    rnd = random.Random(seed)
    obs: List[tuple] = []
    q = 0
    for _ in range(rnd.randint(1, 3)):
        q = min(10, q + rnd.randint(0, 6)) or 1
        obs.append(("E", 0, "partially_filled" if q < 10 else "filled", q, rnd.choice([None, "10.00", "10.30"])))
        if rnd.random() < 0.4:
            obs.append(("E", 0, obs[-1][2], q, rnd.choice([None, "10.10", "10.30"])))        # a re-observation: price-only, or a flap
    obs.append(("E", 0, "filled" if q == 10 else "canceled", q, rnd.choice([None, "10.20"])))
    for cyc in (1, 2, 3):
        for _ in range(rnd.randint(1, 3)):
            obs.append(("X", cyc, "partially_filled", rnd.random(), rnd.choice([None, "11.00", "9.50"])))
            if rnd.random() < 0.3:
                obs.append(("X", cyc, "partially_filled", -1, rnd.choice([None, "11.10", "9.40"])))   # the same quantity again (price-only / None)
        obs.append(("X", cyc, rnd.choice(["filled", "canceled", "expired"]), rnd.random(), rnd.choice([None, "10.50"])))
    if seed % 3 == 0:                                                                        # checkpoint 1 K-12: keep observing after the close
        for px in (rnd.choice(["10.55", "10.45"]), None, rnd.choice(["10.60", None])):
            obs.append(("C", 0, "filled", 0, px))                                          # the last exit order again: a late price correction or a price-less repeat
    return obs


def decision_trace(d: Direct, seed: int) -> List[dict]:
    """One seeded trace: an entry order observed 1-3 times (prices sometimes missing, sometimes re-reported), then up to three exit cycles each
    observed 1-4 times; after every applied observation the DECISION fields (never a price or a P/L) of the position and of every intent."""
    ent = d.entry(xid=f"{seed:032d}")
    trace, xcur = [], {}
    for kind, cyc, status, cq, px in _trace_obs(seed):
        if kind == "E":
            it = d.xs.intent(ent.execution_id)
            if it.state not in OPEN:
                continue
            d.observe(it, status, cq, px)
        elif kind == "C":
            pos = d.pos(ent)
            if pos is None or pos.state != "CLOSED" or not xcur:
                continue
            xi, done = xcur[max(xcur)]
            it = d.xs.intent(xi.execution_id)
            if it.filled_qty <= 0:
                continue
            d.observe(it, it.broker_status or status, it.filled_qty, px)
        else:
            pos = d.pos(ent)
            if pos is None or pos.state == "CLOSED" or pos.quantity <= 0:
                continue
            if cyc not in xcur:
                if any(i.position_id == pos.position_id and i.intent_type == "EXIT" for i in d.xs.open_intents()) or pos.exit_cycle >= cyc:
                    continue
                xcur[cyc] = (d.exit(pos, f"{seed:028d}x{cyc:03d}", pos.quantity, cycle=cyc), 0)
            xi, done = xcur[cyc]
            it = d.xs.intent(xi.execution_id)
            if it.state not in OPEN:
                continue
            cum = xi.qty if status == "filled" else done if cq == -1 else done + int(cq * (xi.qty - done))
            xcur[cyc] = (xi, cum)
            d.observe(it, status, cum, px)
        p = d.pos(ent)
        mine = [i for i in d.xs.intents(limit=500) if i.execution_id == ent.execution_id or (p is not None and i.position_id == p.position_id)]
        trace.append({"pos": {k: getattr(p, k) for k in DEC_POS} if p else None,
                      "intents": sorted([i.execution_id, *[getattr(i, k) for k in DEC_INT]] for i in mine),
                      "open": sorted(i.execution_id for i in d.xs.open_intents() if i in mine)})
    return trace


def trace_digest(trace: List[dict]) -> str:
    return hashlib.sha256(json.dumps(trace, sort_keys=True, default=str).encode()).hexdigest()


def decision_golden(tmp_path: Path, seeds: range) -> Dict[str, Any]:
    d = Direct(tmp_path, "golden.db")
    out = {}
    for seed in seeds:
        t = decision_trace(d, seed)
        out[str(seed)] = {"steps": len(t), "sha256": trace_digest(t)}
    return out


# ---------------------------------------------------------------- stack traces (T-A16, T-R05-pre; the golden is taken on 5d6f7dd) ------------
import re as _re  # noqa: E402

_HEX = _re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}|[0-9a-f]{12,}")


def stack_snapshot(st) -> dict:
    """The executor's DECISIONS after a cycle: positions and intents (decision fields; ids replaced by creation order), the open intents, every
    POST that reached the broker, the reconciliation verdict and findings, the mode. No price and no P/L."""
    ints = sorted(st.x.store.intents(limit=500), key=lambda i: (i.created_at, i.symbol, i.intent_type, i.exit_cycle, i.client_order_id))
    pos = sorted(st.x.store.positions(open_only=False, limit=500), key=lambda p: (p.opened_at, p.symbol))
    opens = {i.execution_id for i in st.x.store.open_intents()}
    posts = [json.loads(b) for b in st.sim.posts]
    snap = {"pos": [[p.symbol, *[getattr(p, k) for k in DEC_POS]] for p in pos],
            "intents": [[i.symbol, i.intent_type, i.side, i.qty, i.exit_cycle, *[getattr(i, k) for k in DEC_INT]] for i in ints],
            "open": [n for n, i in enumerate(ints) if i.execution_id in opens],
            "posts": [[b.get("symbol"), b.get("side"), b.get("qty"), b.get("type")] for b in posts],
            "recon": [bool(st.x.recon_healthy), sorted(st.x.recon_reasons or [])],
            "findings": sorted([f["kind"], f.get("symbol"), bool(f.get("blocking")), f.get("trigger")] for f in st.x.store.findings(1000)),
            "mode": st.x.mode()["mode"]}
    return json.loads(_HEX.sub("#", json.dumps(snap, sort_keys=True, default=str)))


def _sim_order(st, symbol: str, side: str, qty: int, coid: str, status: str = "new") -> dict:
    o = st.sim._order_json({"client_order_id": coid, "symbol": symbol, "qty": str(qty), "type": "market", "side": side, "time_in_force": "day"}, status)
    st.sim.orders[o["id"]] = o
    return o


def stack_scenarios():
    """16 executor traces through the REAL stack (setups -> proposals -> safety -> executor -> simulated broker): partial, price-only, unpriced,
    expired, rejected and multi-cycle exits, partial and unpriced entries, a restart, Monitor only mid-trade, an external position."""
    from test_execution_60 import arm, below_invalidation, cyc, entered, go_ready, reopen

    def exit_posted(st, pv):
        st.sim.auto_fill = "manual"
        below_invalidation(st, pv)
        cyc(st)
        return st.x.store.intents(intent_type="EXIT")[0]

    def s01(st, snap):
        entered(st)
        for _ in range(3):
            cyc(st)
            snap()

    def s02(st, snap):
        pv, _ = entered(st)
        below_invalidation(st, pv)
        for _ in range(3):
            cyc(st)
            snap()

    def s03(st, snap):
        pv, pos = entered(st)
        ex = exit_posted(st, pv)
        snap()
        st.sim.fill(ex.client_order_id, max(1, pos.quantity * 4 // 10), float(Decimal(pv.invalidation_price) + 1))
        cyc(st)
        snap()
        st.sim.fill(ex.client_order_id, None, float(Decimal(pv.invalidation_price) - 1))
        cyc(st)
        snap()

    def s04(st, snap):
        pv, pos = entered(st)
        ex = exit_posted(st, pv)
        snap()
        st.sim.fill(ex.client_order_id, max(1, pos.quantity * 4 // 10))
        cyc(st)
        snap()
        st.sim.set_status(ex.client_order_id, "expired")
        cyc(st)
        snap()
        cyc(st)
        snap()
        nxt = [i for i in st.x.store.intents(intent_type="EXIT") if i.exit_cycle == 2]
        if nxt:
            st.sim.fill(nxt[0].client_order_id)
            cyc(st)
            snap()

    def s05(st, snap):
        pv, pos = entered(st)
        ex = exit_posted(st, pv)
        snap()
        st.sim.fill(ex.client_order_id, max(1, pos.quantity // 2), 50.0)
        cyc(st)
        snap()
        st.sim.order_by_coid(ex.client_order_id)["filled_avg_price"] = "49.50"      # a price-only correction at the same quantity
        cyc(st)
        snap()
        st.sim.fill(ex.client_order_id, None, 48.0)
        cyc(st)
        snap()

    def s06(st, snap):
        arm(st)
        st.sim.auto_fill = "manual"
        cyc(st)
        snap()
        it = st.x.store.intents(intent_type="ENTRY")[0]
        st.sim.fill(it.client_order_id, max(1, it.qty // 3))
        cyc(st)
        snap()
        st.sim.fill(it.client_order_id)
        cyc(st)
        snap()
        cyc(st)
        snap()

    def s07(st, snap):
        arm(st)
        st.sim.auto_fill = "manual"
        cyc(st)
        snap()
        it = st.x.store.intents(intent_type="ENTRY")[0]
        st.sim.fill(it.client_order_id, max(1, it.qty // 3))
        cyc(st)
        snap()
        st.sim.set_status(it.client_order_id, "canceled")
        cyc(st)
        snap()
        cyc(st)
        snap()

    def s08(st, snap):
        arm(st)
        st.sim.auto_fill = "manual"
        cyc(st)
        snap()
        it = st.x.store.intents(intent_type="ENTRY")[0]
        st.sim.fill(it.client_order_id)
        o = st.sim.order_by_coid(it.client_order_id)
        px, o["filled_avg_price"] = o["filled_avg_price"], None
        cyc(st)                                                                     # a fill reported without its price
        snap()
        o["filled_avg_price"] = px
        cyc(st)
        snap()
        cyc(st)
        snap()

    def s09(st, snap):
        pv, pos = entered(st)
        ex = exit_posted(st, pv)
        snap()
        st.sim.fill(ex.client_order_id)
        o = st.sim.order_by_coid(ex.client_order_id)
        px, o["filled_avg_price"] = o["filled_avg_price"], None
        cyc(st)
        snap()
        o["filled_avg_price"] = px
        cyc(st)
        snap()

    def s10(st, snap):
        arm(st)
        go_ready(st, "NVDA")
        for _ in range(4):
            cyc(st)
            snap()

    def s11(st, snap):
        pv, pos = entered(st)
        ex = exit_posted(st, pv)
        snap()
        st.sim.fill(ex.client_order_id, max(1, pos.quantity * 4 // 10), 60.0)
        cyc(st)
        snap()
        reopen(st)
        st.sim.fill(ex.client_order_id, None, 58.0)
        cyc(st)
        snap()
        cyc(st)
        snap()

    def s12(st, snap):
        pv, pos = entered(st)
        ex = exit_posted(st, pv)
        snap()
        st.sim.set_status(ex.client_order_id, "rejected")
        for _ in range(3):
            cyc(st)
            snap()

    def s13(st, snap):
        pv, pos = entered(st)
        ex = exit_posted(st, pv)
        snap()
        st.sim.fill(ex.client_order_id, max(1, pos.quantity // 2), 55.0)
        cyc(st)
        snap()
        o = st.sim.order_by_coid(ex.client_order_id)
        px = o["filled_avg_price"]
        for v in (None, px, None):                                                   # the price flaps at an unchanged quantity
            o["filled_avg_price"] = v
            cyc(st)
            snap()
        o["filled_avg_price"] = px
        st.sim.fill(ex.client_order_id)
        cyc(st)
        snap()

    def s14(st, snap):
        pv, pos = entered(st)
        exit_posted(st, pv)
        snap()
        for c in (1, 2):
            cur = [i for i in st.x.store.intents(intent_type="EXIT") if i.exit_cycle == c]
            if not cur:
                break
            st.sim.fill(cur[0].client_order_id, 1)
            cyc(st)
            snap()
            st.sim.set_status(cur[0].client_order_id, "canceled")
            cyc(st)
            snap()
            cyc(st)
            snap()
        for i in st.x.store.open_intents():
            st.sim.fill(i.client_order_id)
        cyc(st)
        snap()

    def s15(st, snap):
        pv, pos = entered(st)
        st.x.disable_auto(actor="user", now=st.clock.now)
        below_invalidation(st, pv)
        for _ in range(3):
            cyc(st)
            snap()

    def s16(st, snap):
        pv, pos = entered(st)
        st.sim.set_position("AMD", 5, 50.0)
        cyc(st)
        snap()
        cyc(st)
        snap()
        below_invalidation(st, pv)
        cyc(st)
        snap()
        cyc(st)
        snap()

    return [s01, s02, s03, s04, s05, s06, s07, s08, s09, s10, s11, s12, s13, s14, s15, s16]


def reconciliation_scenarios():
    """12 reconciliation situations, each followed for 5 cycles (T-R05-pre: PR A leaves _reconcile untouched)."""
    from test_execution_60 import arm, below_invalidation, cyc, entered, reopen

    def r01(st):
        arm(st)
        st.sim.auto_fill = "manual"
        cyc(st)
        del st.sim.orders[st.x.store.intents()[0].broker_order_id]

    def r02(st):
        _, pos = entered(st)
        st.sim.set_position("SNDK", pos.quantity - 1, 100.0)

    def r03(st):
        entered(st)
        st.sim.set_position("SNDK", 0)

    def r04(st):
        entered(st)
        st.sim.set_position("AMD", 4, 50.0)

    def r05(st):
        entered(st)
        st.sim.read_fail_paths = ["/v2/positions"]

    def r06(st):
        entered(st)
        _sim_order(st, "AMD", "buy", 1, "manual-1")
        _sim_order(st, "AMD", "buy", 2, "manual-2")

    def r07(st):
        entered(st)
        _sim_order(st, "SNDK", "buy", 1, "stockagent-entry-" + "f" * 32)

    def r08(st):
        entered(st)
        _sim_order(st, "SNDK", "sell", 1, "manual-sndk")

    def r09(st):
        entered(st)

    def r10(st):
        pv, pos = entered(st)
        st.sim.auto_fill = "manual"
        below_invalidation(st, pv)
        cyc(st)
        st.sim.fill(st.x.store.intents(intent_type="EXIT")[0].client_order_id, max(1, pos.quantity // 2))

    def r11(st):
        pv, pos = entered(st)
        below_invalidation(st, pv)
        cyc(st)
        cyc(st)
        st.sim.set_position("SNDK", 3, 100.0)

    def r12(st):
        entered(st)
        reopen(st)

    return [r01, r02, r03, r04, r05, r06, r07, r08, r09, r10, r11, r12]


def stack_golden(tmp_path: Path) -> Dict[str, Any]:
    from test_execution_60 import cyc, make_stack
    out: Dict[str, Any] = {}
    for fn in stack_scenarios():
        st = make_stack(tmp_path / fn.__name__)
        snaps: List[dict] = []
        fn(st, lambda: snaps.append(stack_snapshot(st)))
        out[fn.__name__] = {"steps": len(snaps), "sha256": trace_digest(snaps)}
    for fn in reconciliation_scenarios():
        st = make_stack(tmp_path / fn.__name__)
        fn(st)
        snaps = []
        for _ in range(5):
            cyc(st)
            snaps.append(stack_snapshot(st))
        out[fn.__name__] = {"steps": len(snaps), "sha256": trace_digest(snaps)}
    return out
