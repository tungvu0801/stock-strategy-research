"""value_quality/audit.py — Value + Quality specific MECHANICAL checks, merged into the run's one audit (the integrity gate blocks on any).

Independent of the strategy core: it imports neither value_quality.core nor value_quality.pit and re-derives, from the stored run record and the
raw dataset (bars + auxiliary point-in-time data), what can be checked without re-implementing the ranking rules:

  REBALANCE_TIMESTAMP   every signal is the FIRST session of its month (and late enough for the liquidity window)
  FACTOR_AVAILABILITY   every feature is dated on / before its signal; every fundamental was filed BEFORE the signal and its available_at is the
                        session after its filing date (the lag re-derived here from the dataset's sessions)
  RESTATEMENT           the balance-sheet values used (assets, equity) are the latest filing for their period visible at the signal
  RANK_SELECTION        the entry rank is within the top N; score = 100 x (value_weight x value + (1 - value_weight) x quality)
  MARKET_CAP_SPLIT      market cap = raw close x reported diluted shares x split ratio(filing) / split ratio(signal) - re-derived from the raw data
  PORTFOLIO_SIZE        at most top N legs at every rebalance (the selection is re-built from the candidates)
  TRANSACTION_FLAGS     entry_trade = not held at the previous rebalance; exit_trade = not held at the next one (friction only on real orders)
  EXIT_AT_REBALANCE     a leg ends on its symbol's first session after the next rebalance (the last rebalance's legs run to the data end)
  NO_OVERLAP            consecutive legs of a stock hand over at the same bar: no duplicate position, no gap"""
from __future__ import annotations

from bisect import bisect_right
from typing import Dict, List

TOL = 1e-6


def _first_sessions(sessions: List[str], warmup: int = 20) -> set:
    return {s for i, s in enumerate(sessions) if i >= warmup and s[:7] != sessions[i - 1][:7]}


def _after(sessions: List[str], d: str) -> str:
    i = bisect_right(sessions, d)
    return sessions[i] if i < len(sessions) else ""


def _latest_instant(rows: List[dict], period_end: str, T: str, sessions: List[str]):
    vis = [r for r in rows if r["end"] == period_end and not r.get("start") and _after(sessions, str(r["filed"])) and _after(sessions, str(r["filed"])) <= T]
    return max(vis, key=lambda r: (r["filed"], r.get("accn") or "")) if vis else None


def audit(record: dict, dataset, top_n: int, value_weight: float) -> List[dict]:
    viol: List[dict] = []
    sessions = dataset.sessions
    firsts = _first_sessions(sessions)
    aux = dataset.aux or {}
    series = aux.get("series") or {}
    close, ratio = series.get("raw_close") or {}, series.get("split_ratio") or {}
    cik_of = {s: cik for cik, info in ((aux.get("static") or {}).get("issuers") or {}).items() for s in info.get("symbols") or []}
    facts = (aux.get("events") or {}).get("facts") or {}
    tag = {"assets": "Assets", "equity": "StockholdersEquity"}
    cands = record.get("candidates") or []

    def v(c, check, detail):
        viol.append({"candidate_id": c["candidate_id"], "check": check, "detail": detail})

    for c in cands:
        T, f, sym = c["signal_date"], c.get("features") or {}, c["symbol"]
        if T not in firsts:
            v(c, "REBALANCE_TIMESTAMP", f"signal {T} is not the first session of its month (after the liquidity window)")
        for k, d in (c.get("feature_as_of") or {}).items():
            if d > T:
                v(c, "FACTOR_AVAILABILITY", f"feature {k} dated {d} after the signal {T}")
        for name, p in (f.get("fundamentals") or {}).items():
            if not (str(p.get("filed")) < T and p.get("available_at") == _after(sessions, str(p.get("filed"))) and p["available_at"] <= T):
                v(c, "FACTOR_AVAILABILITY", f"{name} filed {p.get('filed')} / available {p.get('available_at')} is not usable at {T}")
            if name in tag:
                best = _latest_instant((facts.get(cik_of.get(sym, "")) or {}).get(tag[name]) or [], p["period_end"], T, sessions)
                if best is None or abs(float(best["val"]) - float(p["value"])) > max(1.0, abs(float(best["val"])) * 1e-9) or best["filed"] != p["filed"]:
                    v(c, "RESTATEMENT", f"{name} {p['period_end']}: used {p['value']} filed {p['filed']}, latest visible filing is "
                                        f"{None if best is None else (best['val'], best['filed'])}")
        sh = f.get("diluted_share_count") or {}
        if not (str(sh.get("filed", "9999")) < T and sh.get("available_at") == _after(sessions, str(sh.get("filed"))) and sh["available_at"] <= T):
            v(c, "FACTOR_AVAILABILITY", f"diluted share count filed {sh.get('filed')} is not usable at {T}")
        if not isinstance(f.get("rank"), int) or not 1 <= f["rank"] <= top_n:
            v(c, "RANK_SELECTION", f"entry rank {f.get('rank')} outside the top {top_n}")
        want = 100.0 * (value_weight * float(f.get("value_score") or 0) + (1.0 - value_weight) * float(f.get("quality_score") or 0))
        if abs(want - float(c.get("score") or 0)) > 1e-4 or abs(want - float(f.get("composite_score") or 0)) > 1e-4:
            v(c, "RANK_SELECTION", f"score {c.get('score')} != 100 x weighted value / quality ({want:.6f})")
        rc, rs = close.get(sym) or {}, ratio.get(sym) or {}
        sess = sorted(rs)
        k = bisect_right(sess, str(sh.get("filed", ""))) - 1
        if rc.get(T) != f.get("raw_close") or k < 0 or not rs.get(T):
            v(c, "MARKET_CAP_SPLIT", "raw close or split ratio at the signal / filing not in the raw data")
        else:
            mcap = rc[T] * float(sh["reported"]) * rs[sess[k]] / rs[T]
            if abs(mcap - float(f.get("market_cap") or 0)) > max(1.0, mcap * 1e-6):
                v(c, "MARKET_CAP_SPLIT", f"market cap {f.get('market_cap')} != {mcap:.2f} re-derived (raw close x shares x split re-basing)")

    # legs: the selection at every rebalance is re-built from the candidates themselves
    schedule = sorted(firsts)
    by_r: Dict[str, Dict[str, dict]] = {}
    for c in cands:
        by_r.setdefault(c["signal_date"], {})[c["symbol"]] = c
    for R, legs in sorted(by_r.items()):
        if len(legs) > top_n:
            viol.append({"candidate_id": None, "check": "PORTFOLIO_SIZE", "detail": f"{len(legs)} legs at the {R} rebalance (top {top_n})"})
        k = schedule.index(R) if R in schedule else -1
        prev_r = schedule[k - 1] if k > 0 else None
        next_r = schedule[k + 1] if 0 <= k < len(schedule) - 1 else None
        for sym, c in legs.items():
            held_before = prev_r is not None and sym in by_r.get(prev_r, {})
            held_after = next_r is not None and sym in by_r.get(next_r, {})
            if bool(c.get("entry_trade", True)) != (not held_before):
                v(c, "TRANSACTION_FLAGS", f"{sym} at {R}: entry_trade {c.get('entry_trade')} but held at the previous rebalance = {held_before}")
            if bool(c.get("exit_trade", True)) != (not held_after):
                v(c, "TRANSACTION_FLAGS", f"{sym} at {R}: exit_trade {c.get('exit_trade')} but held at the next rebalance = {held_after}")
            after = dataset.bars_after(sym, R)
            if next_r is None:
                if c["max_hold_bars"] <= len(after):
                    v(c, "EXIT_AT_REBALANCE", f"{sym} at {R}: the last rebalance's leg must run to the data end")
            else:
                want = next((i + 1 for i, b in enumerate(after) if b.date > next_r), len(after) + 1)
                if c["max_hold_bars"] != want:
                    v(c, "EXIT_AT_REBALANCE", f"{sym} at {R}: the leg holds {c['max_hold_bars']} bars, the first session after the next rebalance {next_r} is bar {want}")
            if held_after:                                                       # consecutive legs chain: this exit bar is the next leg's entry bar
                nxt_after = dataset.bars_after(sym, next_r)
                if not nxt_after or c["max_hold_bars"] > len(after) or after[c["max_hold_bars"] - 1].date != nxt_after[0].date:
                    v(c, "NO_OVERLAP", f"{sym}: the leg from {R} does not hand over at the next leg's entry bar")
    return viol
