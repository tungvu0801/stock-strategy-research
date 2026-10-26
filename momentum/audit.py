"""momentum/audit.py — Momentum-specific MECHANICAL checks, merged into the run's one audit (the integrity gate blocks on any violation).

INDEPENDENT of the strategy: it imports neither momentum.core nor momentum.data. It re-derives the whole rule set from the raw dataset (bars +
raw series + issuer map) and the frozen rule constants - momentum's rules are simple enough to re-implement, so the selection itself is audited,
not only its bookkeeping:

  REBALANCE_TIMESTAMP   every signal is a first session of its month, once the lookback exists
  LOOKBACK_ENDPOINTS    the recorded endpoints are sessions i - lookback and i - skip of the dataset calendar (the recent month excluded)
  NO_FUTURE_DATA        every feature dated on / before the signal; both endpoints strictly before it
  MOMENTUM_RETURN       the recorded endpoint closes and the momentum re-derived from the adjusted bars
  SPLIT_CONSISTENCY     on every split session inside the lookback window the adjusted day return equals the split-adjusted raw day return (raw
                        close / split ratio): a split can never create momentum. (The all-adjusted series also carries dividends and spin-off
                        distributions - AT&T / WarnerMedia 2022, GE / GE HealthCare 2023, GE Vernova 2024 - which are total return, not errors.)
  ELIGIBILITY           the eligible set re-derived (price, liquidity, history, endpoint bars, one listing per issuer): its size = eligible_count
  RANK_SELECTION        the selection = the re-derived top N by momentum (ties by symbol); every rank and percentile score re-derived
  PORTFOLIO_SIZE        a leg for every selected stock at every rebalance with an eligible stock
  TRANSACTION_FLAGS     entry_trade = not held at the previous rebalance; exit_trade = not held at the next one
  EXIT_AT_REBALANCE     a leg ends on its symbol's first session after the next rebalance (the last rebalance's legs run to the data end)
  NO_OVERLAP            consecutive legs of a stock hand over at the same bar"""
from __future__ import annotations

import statistics
from bisect import bisect_right
from typing import Dict, List, Optional

from momentum import DOLLAR_VOLUME_SESSIONS, MIN_MEDIAN_DOLLAR_VOLUME, MIN_PRICE, RESEARCH_UNIVERSE

TOL = 1e-6
SPLIT_DAY_TOL = 0.02                              # adjusted vs split-adjusted raw day return on a split session (an ex-dividend the same day at most)
SPLIT_SESSION = 0.01                              # the stored raw / split-adjusted ratio carries rounding noise; a real split moves it by >= ~33 %


def _schedule(sessions: List[str], lookback: int) -> List[str]:
    start = max(lookback, DOLLAR_VOLUME_SESSIONS)
    return [s for i, s in enumerate(sessions) if i >= start and s[:7] != sessions[i - 1][:7]]


def _expected(T: str, i: int, sessions: List[str], closes: Dict[str, Dict[str, float]], raw: Dict[str, Dict[str, float]], vol: Dict[str, Dict[str, float]],
              issuer: Dict[str, str], lookback: int, skip: int) -> Dict[str, float]:
    """The eligible symbols at T and their 12-1 momentum - re-derived from the raw data."""
    start, end = sessions[i - lookback], sessions[i - skip]
    out, seen = {}, set()
    for s in sorted(x for x in RESEARCH_UNIVERSE if x in closes):
        iss = issuer.get(s, s)
        if iss in seen:
            continue
        seen.add(iss)
        c = closes[s]
        px = (raw.get(s) or {}).get(T)
        if px is None or T not in c or px < MIN_PRICE:
            continue
        ds = sorted(d for d in (raw.get(s) or {}) if d <= T)[-DOLLAR_VOLUME_SESSIONS:]
        if len(ds) < DOLLAR_VOLUME_SESSIONS or statistics.median(raw[s][d] * (vol.get(s) or {}).get(d, 0.0) for d in ds) < MIN_MEDIAN_DOLLAR_VOLUME:
            continue
        if not c or min(c) > start or start not in c or end not in c or c[start] <= 0 or c[end] <= 0:
            continue
        out[s] = c[end] / c[start] - 1.0
    return out


def _pct(m: Dict[str, float]) -> Dict[str, float]:
    srt = sorted(m.items(), key=lambda kv: (kv[1], kv[0]))
    n, out, i = len(srt), {}, 0
    while i < n:
        j = i
        while j + 1 < n and srt[j + 1][1] == srt[i][1]:
            j += 1
        for k in range(i, j + 1):
            out[srt[k][0]] = ((i + j) / 2.0 + 1.0) / n
        i = j + 1
    return out


def audit(record: dict, dataset, top_n: int, lookback: int, skip: int) -> List[dict]:
    viol: List[dict] = []
    sessions = dataset.sessions
    pos = {d: i for i, d in enumerate(sessions)}
    sched = _schedule(sessions, lookback)
    sset = set(sched)
    closes = {s: {b.date: b.close for b in rows} for s, rows in dataset.bars.items()}
    series = (dataset.aux or {}).get("series") or {}
    raw, vol, ratio = series.get("raw_close") or {}, series.get("raw_volume") or {}, series.get("split_ratio") or {}
    issuer = {s: cik for cik, info in (((dataset.aux or {}).get("static") or {}).get("issuers") or {}).items() for s in info.get("symbols") or []}
    cands = record.get("candidates") or []

    def v(c, check, detail):
        viol.append({"candidate_id": c["candidate_id"] if c else None, "check": check, "detail": detail})

    by_r: Dict[str, Dict[str, dict]] = {}
    for c in cands:
        by_r.setdefault(c["signal_date"], {})[c["symbol"]] = c
    expected: Dict[str, Dict[str, float]] = {}
    for T in sched:
        expected[T] = _expected(T, pos[T], sessions, closes, raw, vol, issuer, lookback, skip)
        exp = expected[T]
        want = [s for s, _ in sorted(exp.items(), key=lambda kv: (-kv[1], kv[0]))[:top_n]]
        got = sorted(by_r.get(T, {}))
        if sorted(want) != got:
            v(None, "RANK_SELECTION" if got else "PORTFOLIO_SIZE", f"{T}: selection {got[:5]}... != the re-derived top {top_n} {sorted(want)[:5]}... "
                                                                  f"({len(got)} vs {len(want)} legs)")
    for c in cands:
        T, f, sym = c["signal_date"], c.get("features") or {}, c["symbol"]
        if T not in sset:
            v(c, "REBALANCE_TIMESTAMP", f"signal {T} is not a scheduled first session of its month")
            continue
        i = pos[T]
        start, end = sessions[i - lookback], sessions[i - skip]
        if (f.get("lookback_start_date"), f.get("lookback_end_date"), f.get("lookback_sessions"), f.get("skip_sessions")) != (start, end, lookback, skip):
            v(c, "LOOKBACK_ENDPOINTS", f"{sym} at {T}: endpoints {f.get('lookback_start_date')} -> {f.get('lookback_end_date')}, expected {start} -> {end} "
                                       f"({lookback} / {skip} sessions)")
        if not (start < end < T) or any(d > T for d in (c.get("feature_as_of") or {}).values()):
            v(c, "NO_FUTURE_DATA", f"{sym} at {T}: a feature dated after the signal, or an endpoint not before it")
        cs = closes.get(sym) or {}
        a, b = cs.get(start), cs.get(end)
        if a is None or b is None or abs(float(f.get("lookback_start_close") or 0) - a) > TOL or abs(float(f.get("lookback_end_close") or 0) - b) > TOL \
                or abs(float(f.get("momentum_return") or 0) - (b / a - 1.0)) > TOL:
            v(c, "MOMENTUM_RETURN", f"{sym} at {T}: recorded momentum {f.get('momentum_return')} does not re-derive from the adjusted closes")
        rr, rt = raw.get(sym) or {}, ratio.get(sym) or {}
        window = sessions[pos[start]:pos[end] + 1]
        for d0, d1 in zip(window, window[1:]):
            if rt.get(d0) and rt.get(d1) and abs(rt[d1] / rt[d0] - 1.0) > SPLIT_SESSION:       # a split session inside the lookback window
                if not (rr.get(d0) and rr.get(d1) and cs.get(d0) and cs.get(d1)):
                    v(c, "SPLIT_CONSISTENCY", f"{sym}: split on {d1} without both raw and adjusted bars")
                    continue
                adj_day = cs[d1] / cs[d0] - 1.0
                split_day = (rr[d1] / rt[d1]) / (rr[d0] / rt[d0]) - 1.0
                if abs(adj_day - split_day) > SPLIT_DAY_TOL:
                    v(c, "SPLIT_CONSISTENCY", f"{sym} at {T}: split on {d1}: adjusted day return {adj_day:.4f} vs split-adjusted {split_day:.4f}")
        exp = expected.get(T) or {}
        if f.get("eligible_count") != len(exp):
            v(c, "ELIGIBILITY", f"{sym} at {T}: eligible_count {f.get('eligible_count')} != {len(exp)} re-derived")
        if sym not in exp:
            v(c, "ELIGIBILITY", f"{sym} at {T}: not eligible when re-derived")
        else:
            order = [s for s, _ in sorted(exp.items(), key=lambda kv: (-kv[1], kv[0]))]
            if f.get("rank") != order.index(sym) + 1 or abs(float(c.get("score") or 0) - 100.0 * _pct(exp)[sym]) > 1e-4:
                v(c, "RANK_SELECTION", f"{sym} at {T}: rank {f.get('rank')} / score {c.get('score')} != re-derived {order.index(sym) + 1} / {100.0 * _pct(exp)[sym]:.4f}")
    # legs: transaction flags, exits and hand-overs re-built from the candidates themselves
    for R, legs in sorted(by_r.items()):
        if R not in sset:
            continue
        k = sched.index(R)
        prev_r = sched[k - 1] if k > 0 else None
        next_r = sched[k + 1] if k < len(sched) - 1 else None
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
                want_h = next((j + 1 for j, b in enumerate(after) if b.date > next_r), len(after) + 1)
                if c["max_hold_bars"] != want_h:
                    v(c, "EXIT_AT_REBALANCE", f"{sym} at {R}: the leg holds {c['max_hold_bars']} bars, the first session after the next rebalance {next_r} is bar {want_h}")
            nxt_after = dataset.bars_after(sym, next_r) if held_after else []
            if nxt_after and (c["max_hold_bars"] > len(after) or after[c["max_hold_bars"] - 1].date != nxt_after[0].date):
                v(c, "NO_OVERLAP", f"{sym}: the leg from {R} does not hand over at the next leg's entry bar")
    return viol
