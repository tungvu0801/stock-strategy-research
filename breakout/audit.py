"""breakout/audit.py — Breakout-specific MECHANICAL checks, merged into the run's one audit (the integrity gate blocks on any violation).

INDEPENDENT of the strategy: it imports neither breakout.core, breakout.data nor breakout.adapter. It re-derives the whole rule set from the raw
dataset (bars + raw series + split ratios + issuer map) and the frozen rule constants, so the selection itself is audited, not only its
bookkeeping:

  REBALANCE_TIMESTAMP   every signal is the first session of its ISO week, once the histories exist; one per week
  REFERENCE_WINDOW      the recorded reference is the highest adjusted high of sessions i - lookback .. i - 1: it ENDS BEFORE T (T's own high never
                        raises its threshold) and starts exactly lookback sessions back
  NO_FUTURE_DATA        every feature dated on / before the signal; the reference strictly before it
  BREAKOUT_EVENT        T's adjusted close is above the reference, and the strength is close / reference - 1
  ELIGIBILITY           the eligible set re-derived (price, liquidity, a complete lookback, positive prices, one listing per issuer, a consistent
                        breakout window): its size = eligible_count and its identity = eligible_set_sha (the selection control's membership)
  CANDIDATE_ACCOUNTING  breakout_candidate_count = the re-derived breakouts; selected_count = min(N, breakouts)
  RANK_SELECTION        the selection = the re-derived N strongest breakouts (ties by symbol) - never a non-breakout; ranks and scores re-derived
  PORTFOLIO_SIZE        a leg for every selected stock at every rebalance; none when nothing breaks out
  CASH_ALLOCATION       fixed slots: the sizing is 1 / N of the equity per stock (never renormalized to the stocks held); no leg's notional above it
  TRANSACTION_FLAGS     entry_trade = not held at the previous rebalance; exit_trade = not held at the next one
  ENTRY_NEXT_OPEN       every simulated leg enters at the open of the stock's first bar after the signal session
  EXIT_AT_REBALANCE     a leg ends at the open of its stock's first bar after the next rebalance (the last rebalance's legs run to the data end)
  NO_OVERLAP            consecutive legs of a stock hand over at the same bar
  SPLIT_CONSISTENCY     for EVERY universe stock over the WHOLE dataset: a REAL split must be absorbed by the adjusted series; a ratio move that is
                        neither a clean split nor a ratio artifact is inconsistent; a RATIO ARTIFACT is reported by the diagnostics, not failed
The period-level cash accounting (strategy return = the sum of the legs / N, cash at 0 %) is re-derived from the stored trades by the integrity
gate (strategy_validation.cross_sectional.verify) and again by the diagnostics."""
from __future__ import annotations

import hashlib
import statistics
from bisect import bisect_left, bisect_right
from datetime import date
from typing import Dict, List

from breakout import (ADJUSTED_LOSS_TOL, DOLLAR_VOLUME_SESSIONS, LARGE_REBASING_TOL, MIN_MEDIAN_DOLLAR_VOLUME, MIN_PRICE, NORMAL_DAY, RESEARCH_UNIVERSE,
                      SPLIT_DAY_TOL, SPLIT_SESSION)

TOL = 1e-6


def schedule(sessions: List[str], lookback: int) -> List[str]:
    start = max(lookback, DOLLAR_VOLUME_SESSIONS)
    wk = [date.fromisoformat(s).isocalendar()[:2] for s in sessions]
    return [s for i, s in enumerate(sessions) if i >= start and wk[i] != wk[i - 1]]


def split_events(dataset, symbols) -> List[dict]:
    """Every move of a stock's raw / split-adjusted ratio between two consecutive bars of the stock (holes included), classified."""
    series = (dataset.aux or {}).get("series") or {}
    raw, ratio = series.get("raw_close") or {}, series.get("split_ratio") or {}
    closes = {s: {b.date: b.close for b in rows} for s, rows in dataset.bars.items()}
    out = []
    for sym in sorted(symbols):
        rr, rt, cs = raw.get(sym) or {}, ratio.get(sym) or {}, closes.get(sym) or {}
        ds_ = sorted(d for d in rt if d in rr)
        for d0, d1 in zip(ds_, ds_[1:]):
            if not (rt[d0] and rt[d1]) or abs(rt[d1] / rt[d0] - 1.0) <= SPLIT_SESSION:
                continue
            ev = {"symbol": sym, "from": d0, "to": d1, "ratio_from": rt[d0], "ratio_to": rt[d1], "raw_return": rr[d1] / rr[d0] - 1.0,
                  "split_adjusted_return": (rr[d1] / rt[d1]) / (rr[d0] / rt[d0]) - 1.0,
                  "adjusted_return": (cs[d1] / cs[d0] - 1.0) if (d0 in cs and d1 in cs) else None}
            raw_jump, spl_jump = abs(ev["raw_return"]) > NORMAL_DAY, abs(ev["split_adjusted_return"]) > NORMAL_DAY
            ev["kind"] = "REAL_SPLIT" if raw_jump and not spl_jump else "RATIO_ARTIFACT" if spl_jump and not raw_jump else "INCONSISTENT"
            out.append(ev)
    return out


class RawData:
    """The raw dataset as plain dictionaries (no strategy code)."""

    def __init__(self, dataset):
        self.sessions = list(dataset.sessions)
        self.pos = {d: i for i, d in enumerate(self.sessions)}
        self.closes = {s: {b.date: b.close for b in rows} for s, rows in dataset.bars.items()}
        self.highs = {s: {b.date: b.high for b in rows} for s, rows in dataset.bars.items()}
        self.bar_dates = {s: sorted(m) for s, m in self.closes.items()}
        series = (dataset.aux or {}).get("series") or {}
        self.raw, self.vol, self.ratio = series.get("raw_close") or {}, series.get("raw_volume") or {}, series.get("split_ratio") or {}
        self.raw_dates = {s: sorted(m) for s, m in self.raw.items()}
        self.issuer = {s: cik for cik, info in (((dataset.aux or {}).get("static") or {}).get("issuers") or {}).items() for s in info.get("symbols") or []}

    def pair_code(self, s: str, d0: str, d1: str):
        """None for a consistent pair of consecutive bars, else why not (the corporate-action contract + the large re-basing rule)."""
        rr, rt, c = self.raw.get(s) or {}, self.ratio.get(s) or {}, self.closes.get(s) or {}
        vals = (rr.get(d0), rr.get(d1), rt.get(d0), rt.get(d1))
        if any(v is None or v <= 0 for v in vals):
            return "UNVERIFIABLE", None
        raw_ret, adj_ret = rr[d1] / rr[d0] - 1.0, c[d1] / c[d0] - 1.0
        if abs(rt[d1] / rt[d0] - 1.0) > SPLIT_SESSION:
            spl = (rr[d1] / rt[d1]) / (rr[d0] / rt[d0]) - 1.0
            raw_jump, spl_jump = abs(raw_ret) > NORMAL_DAY, abs(spl) > NORMAL_DAY
            if spl_jump and not raw_jump:
                return "RATIO_ARTIFACT", (1.0 + adj_ret) / (1.0 + raw_ret) - 1.0
            if not (raw_jump and not spl_jump):
                return "INCONSISTENT_SPLIT", (1.0 + adj_ret) / (1.0 + raw_ret) - 1.0
            if abs(adj_ret - spl) > SPLIT_DAY_TOL:
                return "SPLIT_NOT_ABSORBED", (1.0 + adj_ret) / (1.0 + spl) - 1.0
            return None, None
        if adj_ret < raw_ret - ADJUSTED_LOSS_TOL:
            return "ADJUSTED_LOSS_WITHOUT_RAW_MOVE", (1.0 + adj_ret) / (1.0 + raw_ret) - 1.0
        if adj_ret > raw_ret + LARGE_REBASING_TOL:
            return "LARGE_REBASING", (1.0 + adj_ret) / (1.0 + raw_ret) - 1.0
        return None, None

    def consistent(self, s: str, a: str, T: str) -> bool:
        ds = self.bar_dates.get(s) or []
        ds = ds[bisect_left(ds, a):bisect_right(ds, T)]
        return all(self.pair_code(s, d0, d1)[0] is None for d0, d1 in zip(ds, ds[1:]))

    def expected(self, T: str, lookback: int) -> Dict[str, dict]:
        """The eligible symbols at T with their reference, close and breakout state - re-derived from the raw data."""
        i = self.pos[T]
        ref_s = self.sessions[i - lookback:i]
        out, seen = {}, set()
        for s in sorted(x for x in RESEARCH_UNIVERSE if x in self.closes):
            iss = self.issuer.get(s, s)
            if iss in seen:
                continue
            seen.add(iss)
            c, h = self.closes[s], self.highs[s]
            px = (self.raw.get(s) or {}).get(T)
            if px is None or T not in c or px < MIN_PRICE:
                continue
            rd = self.raw_dates.get(s) or []
            ds = rd[max(0, bisect_right(rd, T) - DOLLAR_VOLUME_SESSIONS):bisect_right(rd, T)]
            if len(ds) < DOLLAR_VOLUME_SESSIONS or statistics.median(self.raw[s][d] * (self.vol.get(s) or {}).get(d, 0.0) for d in ds) < MIN_MEDIAN_DOLLAR_VOLUME:
                continue
            if len(ref_s) < lookback or self.bar_dates[s][0] > ref_s[0] or any(d not in h for d in ref_s):
                continue
            hs = [h[d] for d in ref_s]
            if c[T] <= 0 or min(hs) <= 0 or not self.consistent(s, ref_s[0], T):
                continue
            ref = max(hs)
            out[s] = {"reference": ref, "reference_start": ref_s[0], "reference_end": ref_s[-1], "close": c[T], "breakout": c[T] > ref,
                      "strength": c[T] / ref - 1.0}
        return out


def inconsistent_pairs(dataset, symbols) -> List[dict]:
    """Every inconsistent pair of consecutive bars over the WHOLE dataset (for the holding-window disclosure): `step` = the part of the adjusted
    move no raw price explains. A distribution the provider did NOT adjust (adjusted = raw) is invisible here."""
    D, out = RawData(dataset), []
    for s in sorted(x for x in symbols if x in D.closes):
        ds = D.bar_dates.get(s) or []
        for d0, d1 in zip(ds, ds[1:]):
            code, step = D.pair_code(s, d0, d1)
            if code and code != "UNVERIFIABLE":
                out.append({"symbol": s, "from": d0, "to": d1, "code": code, "step": step})
    return out


def _pct(vals: Dict[str, float]) -> Dict[str, float]:
    """Percentiles of the strength (the strongest = 1.0), tie-averaged."""
    srt = sorted(vals.items(), key=lambda kv: (kv[1], kv[0]))
    n, out, i = len(srt), {}, 0
    while i < n:
        j = i
        while j + 1 < n and srt[j + 1][1] == srt[i][1]:
            j += 1
        for k in range(i, j + 1):
            out[srt[k][0]] = ((i + j) / 2.0 + 1.0) / n
        i = j + 1
    return out


def audit(record: dict, dataset, max_holdings: int, lookback: int) -> List[dict]:
    viol: List[dict] = []
    D = RawData(dataset)
    sessions, pos = D.sessions, D.pos
    sched = schedule(sessions, lookback)
    sset = set(sched)
    cands = record.get("candidates") or []

    def v(c, check, detail):
        viol.append({"candidate_id": c["candidate_id"] if c else None, "check": check, "detail": detail})

    weeks = [date.fromisoformat(s).isocalendar()[:2] for s in sched]
    if len(set(weeks)) != len(weeks):
        v(None, "REBALANCE_TIMESTAMP", "more than one rebalance in an ISO week")
    by_r: Dict[str, Dict[str, dict]] = {}
    for c in cands:
        by_r.setdefault(c["signal_date"], {})[c["symbol"]] = c
    expected: Dict[str, Dict[str, dict]] = {}
    breakouts: Dict[str, Dict[str, float]] = {}
    order: Dict[str, List[str]] = {}
    for T in sched:
        expected[T] = exp = D.expected(T, lookback)
        breakouts[T] = bo = {s: x["strength"] for s, x in exp.items() if x["breakout"]}
        order[T] = [s for s, _ in sorted(bo.items(), key=lambda kv: (-kv[1], kv[0]))]
        want = order[T][:max_holdings]
        got = sorted(by_r.get(T, {}))
        if sorted(want) != got:
            v(None, "RANK_SELECTION" if (got and want) else "PORTFOLIO_SIZE",
              f"{T}: selection {got[:5]}... != the re-derived {min(max_holdings, len(bo))} strongest breakouts {sorted(want)[:5]}... ({len(got)} vs {len(want)} legs)")
    sizing = (record.get("config_snapshot") or {}).get("sizing") or {}
    if sizing.get("mode") != "EQUAL_WEIGHT" or abs(float(sizing.get("position_pct") or 0) - 100.0 / max_holdings) > 1e-9:
        v(None, "CASH_ALLOCATION", f"sizing {sizing.get('mode')} {sizing.get('position_pct')} % is not a fixed slot of 1 / {max_holdings}")
    ctl = ((record.get("config_snapshot") or {}).get("adapter") or {}).get("control_definition") or {}
    cap = ctl.get("strategy_capital") or {}
    if (cap.get("basis"), cap.get("slots"), cap.get("cash_return")) != ("FIXED_SLOTS_CASH", max_holdings, 0.0):
        v(None, "CASH_ALLOCATION", f"the declared strategy capital {cap} is not {max_holdings} fixed slots with cash at 0 %")
    for c in cands:
        T, f, sym = c["signal_date"], c.get("features") or {}, c["symbol"]
        if T not in sset:
            v(c, "REBALANCE_TIMESTAMP", f"signal {T} is not a scheduled first session of its ISO week")
            continue
        i = pos[T]
        rs, re_ = sessions[i - lookback], sessions[i - 1]
        if (f.get("reference_start_date"), f.get("reference_end_date"), f.get("lookback_sessions")) != (rs, re_, lookback) or not re_ < T:
            v(c, "REFERENCE_WINDOW", f"{sym} at {T}: reference {f.get('reference_start_date')} -> {f.get('reference_end_date')}, expected {rs} -> {re_} "
                                     f"(the {lookback} sessions BEFORE T)")
        if any(d > T for d in (c.get("feature_as_of") or {}).values()) or (f.get("reference_end_date") or "9") >= T:
            v(c, "NO_FUTURE_DATA", f"{sym} at {T}: a feature dated after the signal, or a reference that reaches T")
        exp = expected.get(T) or {}
        x = exp.get(sym)
        if x is None:
            v(c, "ELIGIBILITY", f"{sym} at {T}: not eligible when re-derived")
        else:
            if abs(float(f.get("breakout_reference") or 0) - x["reference"]) > TOL or abs(float(f.get("close") or 0) - x["close"]) > TOL:
                v(c, "REFERENCE_WINDOW", f"{sym} at {T}: recorded reference {f.get('breakout_reference')} / close {f.get('close')} != re-derived {x['reference']} / {x['close']}")
            if not x["breakout"] or abs(float(f.get("breakout_strength") or 0) - x["strength"]) > TOL:
                v(c, "BREAKOUT_EVENT", f"{sym} at {T}: no breakout when re-derived, or strength {f.get('breakout_strength')} != {x['strength']}")
        if f.get("eligible_count") != len(exp) or f.get("eligible_set_sha") != hashlib.sha256(",".join(sorted(exp)).encode("utf-8")).hexdigest()[:16]:
            v(c, "ELIGIBILITY", f"{sym} at {T}: eligible_count {f.get('eligible_count')} / set {f.get('eligible_set_sha')} != {len(exp)} re-derived")
        bo = breakouts.get(T) or {}
        if f.get("breakout_candidate_count") != len(bo) or f.get("selected_count") != min(max_holdings, len(bo)):
            v(c, "CANDIDATE_ACCOUNTING", f"{sym} at {T}: {f.get('breakout_candidate_count')} candidates / {f.get('selected_count')} selected != re-derived "
                                         f"{len(bo)} / {min(max_holdings, len(bo))}")
        if sym in bo and (f.get("rank") != order[T].index(sym) + 1 or abs(float(c.get("score") or 0) - 100.0 * _pct(bo)[sym]) > 1e-4):
            v(c, "RANK_SELECTION", f"{sym} at {T}: rank {f.get('rank')} / score {c.get('score')} != re-derived {order[T].index(sym) + 1} / {100.0 * _pct(bo)[sym]:.4f}")
    for ev in split_events(dataset, [x for x in RESEARCH_UNIVERSE if x in D.closes]):    # splits: the whole dataset, every universe stock
        where = f"{ev['symbol']}: ratio {ev['ratio_from']:.4g} -> {ev['ratio_to']:.4g} between {ev['from']} and {ev['to']}"
        if ev["kind"] == "RATIO_ARTIFACT":
            continue                                                               # reported by the diagnostics; never inside a ranked window
        if ev["adjusted_return"] is None:
            v(None, "SPLIT_CONSISTENCY", f"{where}: no adjusted bars on both sides")
        elif ev["kind"] == "INCONSISTENT":
            v(None, "SPLIT_CONSISTENCY", f"{where}: raw {ev['raw_return']:.4f} / split-adjusted {ev['split_adjusted_return']:.4f}: neither a clean split nor a "
                                          "ratio artifact")
        elif abs(ev["adjusted_return"] - ev["split_adjusted_return"]) > SPLIT_DAY_TOL:
            v(None, "SPLIT_CONSISTENCY", f"{where}: adjusted return {ev['adjusted_return']:.4f} vs split-adjusted {ev['split_adjusted_return']:.4f} (a split "
                                          "not absorbed by the adjusted series)")
    # legs: transaction flags, entries, exits, weights and hand-overs re-built from the candidates and the simulated trades
    trades = {t["candidate_id"]: t for t in (((record.get("arms") or {}).get("RULES") or {}).get("trades") or [])}
    equity = float(sizing.get("equity") or 100000.0)
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
            t = trades.get(c["candidate_id"])
            if t is None or not after:
                continue
            if t["entry_date"] != after[0].date or abs(float(t["entry_raw"]) - after[0].open) > TOL:
                v(c, "ENTRY_NEXT_OPEN", f"{sym} at {R}: entered {t['entry_date']} at {t['entry_raw']}, expected the open {after[0].open} of {after[0].date}")
            x = next((b for b in after if b.date > next_r), None) if next_r is not None else None
            if x is not None and (t["exit_date"] != x.date or abs(float(t["exit_raw"]) - x.open) > TOL):
                v(c, "EXIT_AT_REBALANCE", f"{sym} at {R}: exited {t['exit_date']} at {t['exit_raw']}, expected the open {x.open} of {x.date}")
            if float(t["entry_fill"]) * float(t["qty"]) > equity / max_holdings * (1 + 1e-9):
                v(c, "CASH_ALLOCATION", f"{sym} at {R}: notional {float(t['entry_fill']) * float(t['qty']):.2f} above the fixed slot 1 / {max_holdings} of {equity:.0f}")
    return viol
