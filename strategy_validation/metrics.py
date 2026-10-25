"""strategy_validation/metrics.py — per arm and per segment (TRAIN, OOS, ALL), computed from the simulated trades only. R is the net P/L divided by the
achieved initial risk (qty x (entry fill - stop)). Expectancy = mean net R (and mean net P/L). t = mean(R) / (std(R) / sqrt(n)) with the sample standard
deviation - evidence, never an oracle. Profit factor is None without a losing trade; the per-trade Sharpe is None below MIN_SHARPE_TRADES. Max drawdown
is measured on the hypothetical equity curve of net P/L in exit order (fixed starting equity, no compounding, overlapping trades allowed)."""
from __future__ import annotations

import math
from collections import Counter
from typing import List, Optional

from strategy_validation import MIN_SHARPE_TRADES
from strategy_validation.models import SimulatedTrade


def _r(x: Optional[float], n: int = 6) -> Optional[float]:
    return None if x is None else round(float(x), n)


def mean(xs: List[float]) -> Optional[float]:
    return sum(xs) / len(xs) if xs else None


def stdev(xs: List[float]) -> Optional[float]:
    if len(xs) < 2:
        return None
    m = sum(xs) / len(xs)
    return math.sqrt(sum((x - m) ** 2 for x in xs) / (len(xs) - 1))


def t_stat(xs: List[float]) -> Optional[float]:
    s = stdev(xs)
    if s is None or s == 0:
        return None
    return mean(xs) / (s / math.sqrt(len(xs)))


def max_drawdown(trades: List[SimulatedTrade], equity: float) -> dict:
    eq, peak, dd, dd_pct = float(equity), float(equity), 0.0, 0.0
    for t in sorted(trades, key=lambda x: (x.exit_date, x.entry_date, x.candidate_id)):
        eq += t.net_pnl
        peak = max(peak, eq)
        if peak - eq > dd:
            dd, dd_pct = peak - eq, (peak - eq) / peak
    return {"max_drawdown": _r(dd), "max_drawdown_pct": _r(dd_pct * 100, 4), "basis": "net P/L in exit order on a fixed hypothetical equity"}


def compute(trades: List[SimulatedTrade], *, candidate_count: int, accepted_count: int, stand_aside_count: int, equity: float,
            boundary_purged_count: int = 0, skipped_count: Optional[int] = None) -> dict:
    n = len(trades)
    R = [t.r_net for t in trades]
    net = [t.net_pnl for t in trades]
    wins, losses = [x for x in net if x > 0], [x for x in net if x < 0]
    gross, slippage, fees = sum(t.gross_pnl for t in trades), sum(t.slippage_cost for t in trades), sum(t.fees for t in trades)
    pf = (sum(wins) / abs(sum(losses))) if losses else None
    rets = [t.trade_return for t in trades]
    sd_ret = stdev(rets)
    sharpe = (mean(rets) / sd_ret) if (n >= MIN_SHARPE_TRADES and sd_ret) else None
    best = max(trades, key=lambda t: t.net_pnl) if trades else None
    worst = min(trades, key=lambda t: t.net_pnl) if trades else None
    out = {
        "trade_count": n, "candidate_count": candidate_count, "accepted_count": accepted_count, "stand_aside_count": stand_aside_count,
        "simulated_trade_count": n, "boundary_purged_count": boundary_purged_count,     # accepted = simulated + boundary_purged + skipped
        "simulation_skipped_count": skipped_count if skipped_count is not None else max(0, accepted_count - n - boundary_purged_count),
        "win_rate": _r(len(wins) / n) if n else None,
        "gross_pnl": _r(gross), "slippage_cost": _r(slippage), "fees": _r(fees), "friction_total": _r(slippage + fees), "net_pnl": _r(sum(net)),
        "average_trade_return": _r(mean(rets)), "average_R": _r(mean(R)), "sum_R": _r(sum(R)), "expectancy_R": _r(mean(R)), "expectancy_usd": _r(mean(net)),
        "average_R_gross": _r(mean([t.r_gross for t in trades])),
        "profit_factor": _r(pf), "profit_factor_note": "no losing trade" if (n and not losses) else "",
        "sharpe_per_trade": _r(sharpe), "sharpe_note": "" if sharpe is not None else f"not reported below {MIN_SHARPE_TRADES} trades (not statistically meaningful)",
        "average_holding_bars": _r(mean([t.holding_bars for t in trades]), 3),
        "best_trade": None if best is None else {"candidate_id": best.candidate_id, "symbol": best.symbol, "net_pnl": best.net_pnl, "r_net": best.r_net},
        "worst_trade": None if worst is None else {"candidate_id": worst.candidate_id, "symbol": worst.symbol, "net_pnl": worst.net_pnl, "r_net": worst.r_net},
        "exit_reason_distribution": dict(sorted(Counter(t.exit_reason for t in trades).items())),
        "mean_R": _r(mean(R)), "std_R": _r(stdev(R)), "mean_R_t_stat": _r(t_stat(R), 4), "sample_size": n,
        "ambiguous_bars_count": sum(1 for t in trades if t.ambiguous_bar),
        "size_status": dict(sorted(Counter(t.size_status for t in trades).items())),
        "configured_risk_total": _r(sum(t.configured_risk for t in trades)), "achieved_risk_total": _r(sum(t.achieved_risk for t in trades)),
    }
    out.update(max_drawdown(trades, equity))
    return out
