"""strategy_validation/audit.py — the MECHANICAL TRADE AUDIT: an independent pass over the generated records AFTER simulation.

It imports nothing from the simulator or the engine. It re-reads the stored candidates and trades plus the raw dataset bars and re-derives, with
its own implementation of the documented rules, whether every trade is what the strategy and the execution assumptions allow: the candidate exists
and its id re-hashes to itself; symbol, direction, strategy id and version match; the entry happened after the signal was known at the documented
fill (next-bar open, or the signal close for a declared market-on-close strategy); the stop equals the candidate's invalidation and lies below the
entry; the exit reason is allowed and consistent with the bar; the exit is the FIRST bar the documented rules exit on (re-derived bar by bar here,
stop first inside an ambiguous bar); the holding period is within the limit; dates are ordered; the TRAIN / OOS label follows the signal date and the
split; no candidate is executed twice in one arm; the arm accepted (TAKE) the candidate, and every accepted candidate has a trade or a recorded
simulation skip (nothing silently dropped); the risk-based size re-derives; gross, friction, net and R arithmetic add up. Result: N / N trades
validated, plus every violation."""
from __future__ import annotations

import hashlib
import json
import math
from typing import Dict, List

ALLOWED_EXITS = {"STOP", "STOP_GAP", "TARGET", "TIME_EXIT", "DATA_END"}
TOL = 1e-6


def _rehash(c: dict) -> str:
    fh = hashlib.sha256(json.dumps({"features": c.get("features") or {}, "as_of": c.get("feature_as_of") or {}}, sort_keys=True, separators=(",", ":"), default=str)
                        .encode("utf-8")).hexdigest()[:32]
    blob = json.dumps([c["strategy_id"], c["strategy_version"], c["symbol"], c["direction"], c["signal_date"], round(float(c["entry_reference"]), 8),
                       round(float(c["invalidation_reference"]), 8), fh], separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:32], fh


def _first_exit(rows: List[dict], si: int, policy: str, stop: float, target, limit: int):
    """The documented exit rules, re-implemented: (exit date, reason, bars held, ambiguous) or None when there is no bar to exit on."""
    seq = rows[si + 1: si + 1 + limit]                                          # next-bar-open: the entry bar is bar 1; market-on-close: the bar after the signal
    for k, b in enumerate(seq, start=1):
        entry_bar = policy == "NEXT_BAR_OPEN" and k == 1
        if not entry_bar and b["open"] <= stop:
            return b["date"], "STOP_GAP", k, False
        if not entry_bar and target is not None and b["open"] >= target:
            return b["date"], "TARGET", k, False
        lo, hi = b["low"] <= stop, target is not None and b["high"] >= target
        if lo:
            return b["date"], "STOP", k, hi                                    # both inside one bar: stop first, flagged ambiguous
        if hi:
            return b["date"], "TARGET", k, False
        if k == limit:
            return b["date"], "TIME_EXIT", k, False
    return (seq[-1]["date"], "DATA_END", len(seq), False) if seq else None


def audit(run: dict, candidates: List[dict], trades: List[dict], bars: Dict[str, List[dict]]) -> dict:
    """run: the stored run (strategy_id, strategy_version, execution_assumptions, friction_model); bars: {symbol: [{date, open, high, low, close}]}."""
    viol: List[dict] = []
    by_id = {}
    for c in candidates:
        cid, fh = _rehash(c)
        if cid != c["candidate_id"] or fh != c.get("feature_snapshot_hash"):
            viol.append({"candidate_id": c["candidate_id"], "check": "CANDIDATE_IDENTITY", "detail": "the candidate id does not re-hash from its identity fields"})
        if c["strategy_id"] != run["strategy_id"] or c["strategy_version"] != run["strategy_version"]:
            viol.append({"candidate_id": c["candidate_id"], "check": "STRATEGY_VERSION", "detail": f"candidate of {c['strategy_id']} v{c['strategy_version']}"})
        by_id[c["candidate_id"]] = c
    idx = {s: {b["date"]: i for i, b in enumerate(rows)} for s, rows in bars.items()}
    ex = run.get("execution_assumptions") or {}
    policy = ex.get("entry_policy", "NEXT_BAR_OPEN")
    slip = float((run.get("friction_model") or {}).get("slippage_bps_per_side", 0.0)) / 10000.0
    arms = run.get("arms") or {}
    takes = {a: {d["candidate_id"] for d in (r.get("decisions") or []) if d.get("decision") == "TAKE"} for a, r in arms.items()}
    sizing = (run.get("config_snapshot") or {}).get("sizing")
    for a, r in arms.items():                                                   # completeness: an accepted candidate never disappears silently
        traded = {t["candidate_id"] for t in trades if t["arm"] == a}
        skipped = {s.get("candidate_id") for s in (r.get("simulation_skips") or [])}
        for cid in sorted(takes[a] - traded - skipped):
            viol.append({"candidate_id": cid, "arm": a, "check": "MISSING_TRADE", "detail": f"{a} accepted the candidate but it has neither a trade nor a recorded skip"})
    seen = set()
    ok = 0
    for t in trades:
        bad = []
        c = by_id.get(t["candidate_id"])
        if c is None:
            viol.append({"trade_id": t["trade_id"], "check": "CANDIDATE_EXISTS", "detail": "the trade has no generated candidate"})
            continue
        if arms and t["candidate_id"] not in takes.get(t["arm"], set()):
            bad.append(("NOT_ACCEPTED", f"{t['arm']} did not accept (TAKE) this candidate"))
        key = (t["arm"], t["candidate_id"])
        if key in seen:
            bad.append(("DUPLICATE_EXECUTION", "the candidate was executed twice in one arm"))
        seen.add(key)
        if t["symbol"] != c["symbol"]:
            bad.append(("SYMBOL", f"{t['symbol']} != candidate {c['symbol']}"))
        if t["direction"] != c["direction"] or t["direction"] != "LONG":
            bad.append(("DIRECTION", f"{t['direction']} vs candidate {c['direction']}"))
        if t["strategy_id"] != run["strategy_id"] or t["strategy_version"] != run["strategy_version"]:
            bad.append(("STRATEGY_VERSION", f"trade of {t['strategy_id']} v{t['strategy_version']} in a run of v{run['strategy_version']}"))
        rows, pos = bars.get(t["symbol"]) or [], idx.get(t["symbol"]) or {}
        si = pos.get(c["signal_date"])
        if si is None:
            bad.append(("SIGNAL_BAR", "the signal session is not in the dataset"))
        else:
            if policy == "NEXT_BAR_OPEN":
                if si + 1 >= len(rows) or rows[si + 1]["date"] != t["entry_date"]:
                    bad.append(("ENTRY_RULE", "the entry is not the first bar after the signal"))
                elif abs(rows[si + 1]["open"] - t["entry_raw"]) > TOL:
                    bad.append(("ENTRY_PRICE", "the entry price is not that bar's open"))
            elif policy == "SIGNAL_CLOSE_MOC":
                if not ex.get("moc_declared") or t["entry_date"] != c["signal_date"] or abs(rows[si]["close"] - t["entry_raw"]) > TOL:
                    bad.append(("ENTRY_RULE", "a signal-close entry without the declared market-on-close assumption or at another price"))
            else:
                bad.append(("ENTRY_RULE", f"unknown entry policy {policy}"))
        if not (t["signal_date"] == c["signal_date"] and (t["entry_date"] > t["signal_date"] or (policy == "SIGNAL_CLOSE_MOC" and t["entry_date"] == t["signal_date"]))):
            bad.append(("TRADE_BEFORE_SIGNAL", f"entry {t['entry_date']} vs signal {c['signal_date']}"))
        if not (t["entry_date"] <= t["exit_date"]) or (policy == "SIGNAL_CLOSE_MOC" and not t["exit_date"] > t["entry_date"]):
            bad.append(("TIMESTAMP_ORDER", f"entry {t['entry_date']} / exit {t['exit_date']}"))
        if abs(t["stop"] - float(c["invalidation_reference"])) > TOL or not t["stop"] < t["entry_raw"]:
            bad.append(("STOP_CONSISTENCY", f"stop {t['stop']} vs invalidation {c['invalidation_reference']} / entry {t['entry_raw']}"))
        if t["exit_reason"] not in ALLOWED_EXITS:
            bad.append(("EXIT_REASON", f"{t['exit_reason']} is not an allowed exit"))
        if t["holding_bars"] > t["max_hold_bars"] or t["max_hold_bars"] != c["max_hold_bars"] or t["holding_bars"] < 1:
            bad.append(("HOLDING_PERIOD", f"{t['holding_bars']} bars vs limit {c['max_hold_bars']}"))
        xi = pos.get(t["exit_date"])
        if xi is None:
            bad.append(("EXIT_BAR", "the exit session is not in the dataset"))
        else:
            xb = rows[xi]
            r = t["exit_reason"]
            if r == "STOP" and not (xb["low"] <= t["stop"] + TOL and abs(t["exit_raw"] - t["stop"]) <= TOL):
                bad.append(("EXIT_CONSISTENCY", "STOP exit without the bar reaching the stop"))
            elif r == "STOP_GAP" and not (xb["open"] <= t["stop"] + TOL and abs(t["exit_raw"] - xb["open"]) <= TOL):
                bad.append(("EXIT_CONSISTENCY", "STOP_GAP exit without a gap through the stop"))
            elif r == "TARGET" and not (t["target"] is not None and xb["high"] >= t["target"] - TOL and abs(t["exit_raw"] - t["target"]) <= TOL):
                bad.append(("EXIT_CONSISTENCY", "TARGET exit without the bar reaching the target"))
            elif r == "TIME_EXIT" and not (t["holding_bars"] == t["max_hold_bars"] and abs(t["exit_raw"] - xb["close"]) <= TOL):
                bad.append(("EXIT_CONSISTENCY", "TIME_EXIT before the holding limit or not at the close"))
            elif r == "DATA_END" and not (xi == len(rows) - 1 and abs(t["exit_raw"] - xb["close"]) <= TOL):
                bad.append(("EXIT_CONSISTENCY", "DATA_END exit before the last bar"))
        if abs(t["entry_fill"] - t["entry_raw"] * (1 + slip)) > 1e-4 or abs(t["exit_fill"] - t["exit_raw"] * (1 - slip)) > 1e-4:
            bad.append(("FRICTION", "fills do not apply the configured slippage"))
        if abs(t["net_pnl"] - (t["gross_pnl"] - t["slippage_cost"] - t["fees"])) > 1e-4:
            bad.append(("FRICTION", "net P/L != gross - slippage - fees"))
        if abs(t["gross_pnl"] - (t["exit_raw"] - t["entry_raw"]) * t["qty"]) > 1e-4 * max(1, t["qty"]):
            bad.append(("GROSS", "gross P/L != (exit - entry) x quantity"))
        ef = t["entry_raw"] * (1 + slip)                                         # the unrounded fill (the record keeps 6 decimals)
        if sizing and ef > t["stop"]:
            q_risk = math.floor(float(sizing["equity"]) * float(sizing["risk_fraction"]) / (ef - t["stop"]))
            q_cap = math.floor(float(sizing["equity"]) * float(sizing["max_position_pct"]) / 100.0 / ef)
            if t["qty"] != max(1, min(q_risk, q_cap)):
                bad.append(("SIZE", f"quantity {t['qty']} != the risk-based size {max(1, min(q_risk, q_cap))}"))
        if t["target"] is not None and not t["target"] > t["entry_raw"]:
            bad.append(("TARGET_CONSISTENCY", f"target {t['target']} not above the entry {t['entry_raw']}"))
        risk = t["qty"] * (t["entry_fill"] - t["stop"])
        if risk <= 0 or abs(t["achieved_risk"] - risk) > 1e-4 * max(1, t["qty"]) or abs(t["r_net"] - t["net_pnl"] / risk) > 1e-4:
            bad.append(("R_MULTIPLE", "R is not net P/L / (quantity x (entry fill - stop))"))
        if run.get("train_end") and t.get("segment") != ("TRAIN" if c["signal_date"] <= run["train_end"] else "OOS"):
            bad.append(("SEGMENT", f"labelled {t.get('segment')} but the signal {c['signal_date']} is {'on or before' if c['signal_date'] <= run['train_end'] else 'after'} the TRAIN end {run['train_end']}"))
        if si is not None and t["stop"] < t["entry_raw"]:
            exp = _first_exit(rows, si, policy, t["stop"], t["target"], c["max_hold_bars"])
            if exp is None or (exp[0], exp[1], exp[2]) != (t["exit_date"], t["exit_reason"], t["holding_bars"]) or exp[3] != bool(t.get("ambiguous_bar")):
                bad.append(("EXIT_FIRST_TOUCH", f"expected {exp[:3] if exp else 'no exit'}{' (ambiguous)' if exp and exp[3] else ''}, recorded {(t['exit_date'], t['exit_reason'], t['holding_bars'])}"))
        if bad:
            viol.extend({"trade_id": t["trade_id"], "candidate_id": t["candidate_id"], "check": k, "detail": d} for k, d in bad)
        else:
            ok += 1
    status = "PASS" if not viol else "FAIL"
    return {"status": status, "checked": len(trades), "validated": ok, "violations": viol, "candidates_checked": len(candidates),
            "note": f"{ok} / {len(trades)} trades validated independently" + ("" if not viol else f"; {len(viol)} violation(s)")}
