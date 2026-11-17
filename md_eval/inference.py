"""md_eval/inference.py - inference on daily series, every parameter from the pre-registration and every seed recorded:
  stationary_bootstrap   Politis-Romano (1994): blocks of geometric length with mean L, wrapping; the statistic is the mean; the one-sided
                         p-value of mean > 0 is the share of CENTRED resamples (m_b - m) at or above m (the basic bootstrap, named);
                         the bounds are the bootstrap percentiles of m_b
  psr                    probabilistic Sharpe (Bailey & Lopez de Prado 2012) on the per-session Sharpe, with sample skew and kurtosis
  dsr                    deflated Sharpe: PSR against SR* = sqrt(V) ((1 - g) Phi^-1(1 - 1/N) + g Phi^-1(1 - 1/(N e))), N = the registry's trial
                         count; V = the variance of the trials' Sharpe ratios when at least two are recorded, else (decision D7, labelled
                         DSR_VARIANCE_FALLBACK_ESTIMATOR) the estimator's own variance (1 - g3 SR + (g4 - 1)/4 SR^2) / (n - 1)
  segment_bounds         purge and embargo are applied by the book (book.py: PURGE_BOUNDARY, EMBARGO); each segment starts flat"""
from __future__ import annotations

import math
import random
from statistics import NormalDist
from typing import List, Optional, Sequence

EULER_GAMMA = 0.5772156649015329
_N = NormalDist()


def _mean(xs):
    return math.fsum(xs) / len(xs)


def stationary_bootstrap(x: Sequence[float], mean_block: float, resamples: int, seed: int, percentiles: Sequence[float] = (5.0, 50.0, 95.0)) -> dict:
    xs = [float(v) for v in x]
    n = len(xs)
    if n < 2 or mean_block < 1 or resamples < 1:
        raise ValueError("the bootstrap needs at least 2 observations, a mean block >= 1 and >= 1 resample")
    rng = random.Random(int(seed))
    p = 1.0 / float(mean_block)
    m = _mean(xs)
    means: List[float] = []
    for _ in range(int(resamples)):
        k = rng.randrange(n)
        acc = 0.0
        for _t in range(n):
            acc += xs[k]
            k = rng.randrange(n) if rng.random() < p else (k + 1) % n
        means.append(acc / n)
    srt = sorted(means)
    pct = {}
    for q in percentiles:
        idx = min(len(srt) - 1, max(0, int(math.floor(q / 100.0 * (len(srt) - 1) + 0.5))))
        pct[str(q)] = srt[idx]
    p_value = sum(1 for mb in means if (mb - m) >= m) / len(means)
    return {"statistic": "MEAN", "observed": m, "percentiles": pct, "p_value_one_sided": p_value, "method": "STATIONARY_BOOTSTRAP_BASIC",
            "mean_block": float(mean_block), "resamples": int(resamples), "seed": int(seed), "n": n}


def moments(x: Sequence[float]) -> dict:
    xs = [float(v) for v in x]
    n = len(xs)
    m = _mean(xs)
    sd = math.sqrt(math.fsum((v - m) ** 2 for v in xs) / (n - 1))
    m2 = math.fsum((v - m) ** 2 for v in xs) / n
    m3 = math.fsum((v - m) ** 3 for v in xs) / n
    m4 = math.fsum((v - m) ** 4 for v in xs) / n
    return {"n": n, "mean": m, "sd": sd, "sr": m / sd if sd else None, "skew": m3 / m2 ** 1.5 if m2 else None, "kurtosis": m4 / m2 ** 2 if m2 else None}


def _sr_var(sr: float, skew: float, kurt: float, n: int) -> float:
    return (1.0 - skew * sr + (kurt - 1.0) / 4.0 * sr * sr) / (n - 1)


def psr(x: Sequence[float], sr_star: float = 0.0) -> dict:
    mo = moments(x)
    if mo["sr"] is None:
        return {"psr": None, "reason": "ZERO_VARIANCE"}
    den = math.sqrt(max(1.0 - mo["skew"] * mo["sr"] + (mo["kurtosis"] - 1.0) / 4.0 * mo["sr"] ** 2, 1e-300))
    z = (mo["sr"] - sr_star) * math.sqrt(mo["n"] - 1) / den
    return {"psr": _N.cdf(z), "sr": mo["sr"], "sr_star": sr_star, "skew": mo["skew"], "kurtosis": mo["kurtosis"], "n": mo["n"]}


PER_PERIOD_SR_LIMIT = 1.0      # a per-session Sharpe above 1 would be about 16 annualised: such a value is taken to be annualised and refused


def dsr(x: Sequence[float], n_trials: int, trial_sharpes: Optional[Sequence[float]] = None) -> dict:
    """LOW-8: `trial_sharpes` are PER-PERIOD (per-session, NOT annualised) Sharpe ratios, in the same units as the tested series' own SR
    (mean / sample sd of `x`); a value whose magnitude exceeds PER_PERIOD_SR_LIMIT is refused as an annualised figure."""
    if int(n_trials) < 1:
        raise ValueError("the trial count is at least 1 (this trial)")
    if trial_sharpes is not None and any(abs(float(s)) > PER_PERIOD_SR_LIMIT for s in trial_sharpes):
        raise ValueError("trial_sharpes must be per-period Sharpe ratios (the units of the tested SR); a value above 1 in magnitude looks annualised")
    mo = moments(x)
    if mo["sr"] is None:
        return {"dsr": None, "reason": "ZERO_VARIANCE"}
    labels = []
    if trial_sharpes is not None and len(trial_sharpes) >= 2:
        tm = _mean(trial_sharpes)
        var = math.fsum((s - tm) ** 2 for s in trial_sharpes) / (len(trial_sharpes) - 1)
    else:
        var = _sr_var(mo["sr"], mo["skew"], mo["kurtosis"], mo["n"])
        labels.append("DSR_VARIANCE_FALLBACK_ESTIMATOR")
    N = int(n_trials)
    if N == 1:
        sr0 = 0.0
    else:
        sr0 = math.sqrt(var) * ((1.0 - EULER_GAMMA) * _N.inv_cdf(1.0 - 1.0 / N) + EULER_GAMMA * _N.inv_cdf(1.0 - 1.0 / (N * math.e)))
    out = psr(x, sr0)
    return {"dsr": out["psr"], "sr": out["sr"], "sr_star": sr0, "n_trials": N, "variance": var, "labels": labels}


def segment_bounds(sessions: Sequence[str], segments: dict) -> dict:
    """{label: (first session, last session, count)} from pre-registered date bounds; segments never overlap."""
    out, prev_end = {}, None
    for label in ("TRAIN", "VALIDATION", "TEST"):
        if label not in segments:
            continue
        lo, hi = segments[label]
        if lo > hi or (prev_end is not None and lo <= prev_end):
            raise ValueError(f"segment {label} {lo}..{hi} overlaps or precedes the previous one")
        ss = [s for s in sessions if lo <= s <= hi]
        out[label] = (ss[0] if ss else None, ss[-1] if ss else None, len(ss))
        prev_end = hi
    return out
