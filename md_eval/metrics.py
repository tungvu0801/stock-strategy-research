"""md_eval/metrics.py - performance metrics of an engine result (deterministic conventions; nothing here is a significance claim):
  returns            daily simple returns of the equity curve (the first session's return is 0)
  cagr, volatility   rotation_backtest.metrics (unchanged): (ending / initial)^(252 / n) - 1; sample stdev x sqrt(252)
  sharpe_excess      mean(r - r_cash) / sample stdev(r - r_cash) x sqrt(252); None when the stdev is 0
  drawdown           rotation_backtest.metrics.max_drawdown: depth (<= 0) and the longest peak-to-recovery span in sessions
  time_in_market     the share of sessions with gross long > 0; mean_exposure = mean(gross long / equity)
  turnover           traded notional (both sides) / mean equity x 252 / n (annualised)
  alpha / beta       OLS of (r - r_cash) on (r_SPY - r_cash), Newey-West (Bartlett) standard errors, lag floor(4 (n / 100)^(2/9)) unless given"""
from __future__ import annotations

import math
from typing import List, Optional, Sequence

from rotation_backtest.metrics import annualized_volatility, cagr, max_drawdown

SESSIONS_PER_YEAR = 252


def daily_returns(equity: Sequence[float]) -> List[float]:
    out = [0.0]
    for a, b in zip(equity, equity[1:]):
        out.append(b / a - 1.0 if a > 0 else 0.0)
    return out


def _mean(xs):
    return math.fsum(xs) / len(xs) if xs else None


def _sd(xs):
    if len(xs) < 2:
        return None
    m = _mean(xs)
    return math.sqrt(math.fsum((x - m) ** 2 for x in xs) / (len(xs) - 1))


def sharpe_excess(returns: Sequence[float], cash_returns: Sequence[float]) -> Optional[float]:
    ex = [r - c for r, c in zip(returns, cash_returns)]
    sd = _sd(ex)
    if not sd:
        return None
    return _mean(ex) / sd * math.sqrt(SESSIONS_PER_YEAR)


def summary(result: dict) -> dict:
    rows = result["rows"]
    eq = [r["equity"] for r in rows]
    rets = daily_returns(eq)
    cash = [r["cash_return"] for r in rows]
    n = len(rows) - 1
    dd = max_drawdown(eq)
    mean_eq = _mean(eq) if eq else None
    return {"n_sessions": len(rows), "total_return": eq[-1] / result["initial_cash"] - 1.0, "cagr": cagr(eq, result["initial_cash"]),
            "volatility": annualized_volatility(rets), "sharpe_excess": sharpe_excess(rets, cash), "max_drawdown": dd["max_drawdown"],
            "max_drawdown_sessions": dd["max_drawdown_sessions"], "time_in_market": sum(1 for r in rows if r["gross_long"] > 0) / len(rows),
            "mean_exposure": _mean([r["exposure"] for r in rows]),
            "turnover_annualised": (result["traded_notional"] / mean_eq * SESSIONS_PER_YEAR / n) if (mean_eq and n > 0) else None,
            "total_costs": result["total_costs"], "flags": list(result["flags"])}


def newey_west_lag(n: int) -> int:
    return int(math.floor(4.0 * (n / 100.0) ** (2.0 / 9.0)))


def alpha_beta(y: Sequence[float], x: Sequence[float], lag: Optional[int] = None) -> dict:
    """OLS y = a + b x with Newey-West (Bartlett kernel) HAC standard errors; lag 0 is White's heteroskedasticity-robust estimator."""
    n = len(y)
    if n != len(x) or n < 3:
        raise ValueError("alpha_beta needs two equal series of at least 3 observations")
    L = newey_west_lag(n) if lag is None else int(lag)
    mx, my = _mean(x), _mean(y)
    sxx = math.fsum((xi - mx) ** 2 for xi in x)
    if sxx == 0:
        raise ValueError("the regressor is constant")
    b = math.fsum((xi - mx) * (yi - my) for xi, yi in zip(x, y)) / sxx
    a = my - b * mx
    e = [yi - a - b * xi for xi, yi in zip(x, y)]
    s00 = s01 = s11 = 0.0                                    # S = sum_t e_t^2 z_t z_t' + sum_l w_l sum_t e_t e_{t-l} (z_t z_{t-l}' + z_{t-l} z_t')
    for t in range(n):
        s00 += e[t] * e[t]
        s01 += e[t] * e[t] * x[t]
        s11 += e[t] * e[t] * x[t] * x[t]
    for l in range(1, L + 1):
        w = 1.0 - l / (L + 1.0)
        for t in range(l, n):
            g = e[t] * e[t - l]
            s00 += w * 2.0 * g
            s01 += w * g * (x[t] + x[t - l])
            s11 += w * 2.0 * g * x[t] * x[t - l]
    sx, sxx_raw = math.fsum(x), math.fsum(xi * xi for xi in x)       # (Z'Z) = [[n, sx], [sx, sxx_raw]]
    det = n * sxx_raw - sx * sx
    i00, i01, i11 = sxx_raw / det, -sx / det, n / det
    # V = (Z'Z)^-1 S (Z'Z)^-1
    m00 = i00 * s00 + i01 * s01
    m01 = i00 * s01 + i01 * s11
    m10 = i01 * s00 + i11 * s01
    m11 = i01 * s01 + i11 * s11
    v00 = m00 * i00 + m01 * i01
    v11 = m10 * i01 + m11 * i11
    se_a, se_b = math.sqrt(max(v00, 0.0)), math.sqrt(max(v11, 0.0))
    return {"alpha_daily": a, "alpha_annualised": a * SESSIONS_PER_YEAR, "beta": b, "se_alpha": se_a, "se_beta": se_b,
            "t_alpha": a / se_a if se_a else None, "t_beta": b / se_b if se_b else None, "lag": L, "n": n, "method": "OLS_NEWEY_WEST_BARTLETT"}
