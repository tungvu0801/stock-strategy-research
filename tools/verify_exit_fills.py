"""
tools/verify_exit_fills.py - Wave 2 PR A OFFLINE check of historical exit-fill accounting (WAVE2_REMEDIATION_DESIGN.md section 3.1.6; audit PF-09).

Before Wave 2 PR A the executor priced each exit increment at the order's CUMULATIVE average, so a position whose exit order was observed more
than once (a partial, then the rest) can carry a wrong average exit price and realized P/L. This tool re-derives, for every paper position with
exit fills, the exit P/L from the broker's own cumulative view of each exit order - the LATEST persisted observation in execution_order_events
(filled_qty, filled_avg_price; a price-less latest view at an unchanged quantity keeps that quantity's latest price, as the executor does) - with the formula PR A uses (execution.service.recompute_pnl: N = sum(A_o * Q_o); tests pin the two equal), and lists every position
whose stored average exit price or realized P/L differs, or that cannot be re-derived (an exit order without a price, or quantities that do not
add up). It never corrects anything: the executor's rows stay as they are. Its verdict is evidence for review beside Wave 2's own per-trade
verification (GET /api/forward-validation/closed: verified, unverified_reasons), which re-derives from the intent rows.

Read only by construction: the database is opened with SQLite's mode=ro URI and nothing else is touched. It is meant to run on a BYTE COPY of the
production database (operator decision D-W2-k), never on the live file. No network, no broker, no credential, no application import beyond the
executor's pure money arithmetic (execution.engine), so importing it creates no database and reads no configuration.

Usage: python -B -m tools.verify_exit_fills --db <byte copy of stock_agent.db> [--json]
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from decimal import Decimal
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from execution.engine import D, money


def rederive(entry_avg, exit_qty: int, exits: List[Tuple[int, Any]]) -> Tuple[Optional[str], Optional[str], bool]:
    """(average exit price, realized P/L, valid): execution.service.recompute_pnl's arithmetic, without importing the executor."""
    e = D(entry_avg)
    filled = [(int(q), D(a)) for q, a in exits if int(q or 0) > 0]
    valid = e is not None and e > 0 and all(a is not None and a > 0 for _, a in filled) and sum(q for q, _ in filled) == exit_qty
    n = sum((a * q for q, a in filled), Decimal(0)) if valid else None
    return (money(n / exit_qty) if valid and exit_qty > 0 else None, money(n - e * exit_qty) if valid and exit_qty > 0 else None, valid)


def _connect(db: Path) -> sqlite3.Connection:
    if not db.exists():
        raise FileNotFoundError(f"no database at {db}")
    conn = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True, timeout=10.0)
    conn.row_factory = sqlite3.Row
    return conn


def check(db: Path) -> Dict[str, Any]:
    """Every paper position with at least one filled exit intent: stored vs re-derived exit P/L, and the verdict."""
    conn = _connect(db)
    try:
        conn.execute("BEGIN")
        positions = conn.execute("SELECT position_id, symbol, state, entry_qty, exit_qty, average_entry_price, average_exit_price, realized_pnl FROM paper_positions "
                                 "WHERE exit_qty > 0 ORDER BY opened_at, rowid").fetchall()
        rows: List[dict] = []
        for p in positions:
            exits = conn.execute("SELECT execution_id, broker_order_id, exit_cycle, filled_qty, filled_avg_price FROM execution_intents "
                                 "WHERE position_id = ? AND intent_type = 'EXIT' ORDER BY exit_cycle, rowid", (p["position_id"],)).fetchall()
            views, orders = [], []
            for x in exits:
                evs = conn.execute("SELECT filled_qty, filled_avg_price, at FROM execution_order_events WHERE execution_id = ? ORDER BY rowid DESC",
                                   (x["execution_id"],)).fetchall()
                ev = evs[0] if evs else None
                if ev is not None and ev["filled_avg_price"] is None:                     # F-12: a price-less repeat keeps the price of the same quantity
                    ev = next((e for e in evs if int(e["filled_qty"] or 0) == int(ev["filled_qty"] or 0) and e["filled_avg_price"] is not None), ev)
                q, a = (int(ev["filled_qty"] or 0), ev["filled_avg_price"]) if ev is not None else (int(x["filled_qty"] or 0), x["filled_avg_price"])
                views.append((q, a))
                orders.append({"execution_id": x["execution_id"], "exit_cycle": x["exit_cycle"], "filled_qty": q, "filled_avg_price": a,
                               "source": "execution_order_events (latest)" if ev is not None else "execution_intents (no order event)"})
            avg, realized, valid = rederive(p["average_entry_price"], int(p["exit_qty"]), views)
            if not valid:
                verdict = "CANNOT_REDERIVE"
            elif D(realized) == D(p["realized_pnl"]) and D(avg) == D(p["average_exit_price"]):
                verdict = "MATCH"
            else:
                verdict = "DIFFERS"
            diff = (D(realized) - D(p["realized_pnl"])) if verdict == "DIFFERS" and D(p["realized_pnl"]) is not None else None
            rows.append({"position_id": p["position_id"], "symbol": p["symbol"], "state": p["state"], "entry_qty": p["entry_qty"], "exit_qty": p["exit_qty"],
                         "average_entry_price": p["average_entry_price"], "stored_average_exit_price": p["average_exit_price"], "stored_realized_pnl": p["realized_pnl"],
                         "rederived_average_exit_price": avg, "rederived_realized_pnl": realized,
                         "difference": str(diff) if diff is not None else None, "verdict": verdict, "exit_orders": orders})
        conn.execute("COMMIT")
    finally:
        conn.close()
    counts = {k: sum(1 for x in rows if x["verdict"] == k) for k in ("MATCH", "DIFFERS", "CANNOT_REDERIVE")}
    return {"database": str(db), "positions_with_exits": len(rows), "counts": counts, "positions": rows,
            "note": "read only (mode=ro); nothing is corrected - review a DIFFERS or CANNOT_REDERIVE row beside Wave 2's per-trade verification (GET /closed)"}


def _text(out: Dict[str, Any]) -> str:
    lines = [f"Exit-fill accounting check of {out['database']}", f"Positions with exit fills: {out['positions_with_exits']}",
             "  " + ", ".join(f"{k}: {v}" for k, v in out["counts"].items())]
    for r in out["positions"]:
        if r["verdict"] != "MATCH":
            lines.append(f"  {r['verdict']}: {r['symbol']} {r['position_id']} stored {r['stored_average_exit_price']} / {r['stored_realized_pnl']}, "
                         f"re-derived {r['rederived_average_exit_price']} / {r['rederived_realized_pnl']}")
    lines.append(out["note"])
    return "\n".join(lines) + "\n"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Read-only check of historical exit-fill accounting (run on a byte copy).")
    ap.add_argument("--db", required=True, type=Path)
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    out = check(a.db)
    sys.stdout.write(json.dumps(out, indent=1, sort_keys=True) + "\n" if a.json else _text(out))
    return 0 if not out["counts"]["DIFFERS"] and not out["counts"]["CANNOT_REDERIVE"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
