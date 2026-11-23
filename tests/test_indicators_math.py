"""
Unit coverage for the pure indicator-math helpers in analysis/indicators.py.

These functions are side-effect-free numerical helpers (no I/O, no network, no DB, no trading
state). The tests use deterministic synthetic data and cover normal cases, insufficient-history
cases, empty inputs and numeric edge cases. compute_metrics() is intentionally NOT exercised here
(it pulls config + levels/scoring/signals); this file pins only the low-level math.

Added by the SPEED V1 first real-development trial. No production code is changed.
"""
import numpy as np
import pandas as pd
import pytest

from analysis import indicators as ind


# --------------------------------------------------------------------------- percent_change
def test_percent_change_normal():
    assert ind.percent_change(110.0, 100.0) == pytest.approx(10.0)
    assert ind.percent_change(90.0, 100.0) == pytest.approx(-10.0)
    assert ind.percent_change(100.0, 100.0) == pytest.approx(0.0)


def test_percent_change_invalid_previous_returns_none():
    assert ind.percent_change(100.0, 0) is None
    assert ind.percent_change(100.0, None) is None
    assert ind.percent_change(100.0, float("nan")) is None


# --------------------------------------------------------------------------- relative_volume
def test_relative_volume_normal_and_invalid():
    assert ind.relative_volume(200.0, 100.0) == pytest.approx(2.0)
    assert ind.relative_volume(50.0, 100.0) == pytest.approx(0.5)
    assert ind.relative_volume(100.0, None) is None
    assert ind.relative_volume(100.0, 0) is None
    assert ind.relative_volume(100.0, float("nan")) is None


# --------------------------------------------------------------------------- ratio_of_averages
def test_ratio_of_averages_known_value():
    s = pd.Series([1, 1, 1, 1, 1, 1, 1, 1, 2, 2, 2], dtype=float)
    # tail(5) mean = (1+1+2+2+2)/5 = 1.6 ; tail(3) mean = 2.0 ; ratio = 1.25
    assert ind.ratio_of_averages(s, short_window=3, long_window=5) == pytest.approx(1.25)


def test_ratio_of_averages_insufficient_history_returns_none():
    s = pd.Series([1.0, 2.0, 3.0])
    assert ind.ratio_of_averages(s, short_window=2, long_window=5) is None


# --------------------------------------------------------------------------- momentum_pct
def test_momentum_pct_known_value():
    close = pd.Series([100.0, 101.0, 102.0, 103.0, 104.0, 110.0])  # 5 bars ago == 100, current == 110
    assert ind.momentum_pct(close, lookback=5) == pytest.approx(10.0)


def test_momentum_pct_insufficient_history_returns_none():
    close = pd.Series([100.0, 101.0, 102.0])
    assert ind.momentum_pct(close, lookback=5) is None


# --------------------------------------------------------------------------- historical_volatility_pct
def test_historical_volatility_pct_constant_series_is_zero():
    close = pd.Series([50.0] * 40)
    assert ind.historical_volatility_pct(close, window=20) == pytest.approx(0.0)


def test_historical_volatility_pct_more_volatile_is_larger():
    calm = pd.Series([100.0 + (i % 2) * 0.1 for i in range(60)])
    wild = pd.Series([100.0 + (i % 2) * 5.0 for i in range(60)])
    v_calm = ind.historical_volatility_pct(calm, window=30)
    v_wild = ind.historical_volatility_pct(wild, window=30)
    assert v_calm is not None and v_wild is not None
    assert v_wild > v_calm > 0.0


def test_historical_volatility_pct_insufficient_history_returns_none():
    assert ind.historical_volatility_pct(pd.Series([1.0, 2.0, 3.0]), window=20) is None


# --------------------------------------------------------------------------- ema
def test_ema_span_one_is_identity_last_value():
    s = pd.Series([1.0, 2.0, 3.0, 4.0, 5.0])
    out = ind.ema(s, span=1)  # alpha=1 -> each point equals the current value
    assert ind._last_valid(out) == pytest.approx(5.0)


def test_ema_constant_series_equals_constant():
    s = pd.Series([7.0] * 10)
    assert ind._last_valid(ind.ema(s, span=5)) == pytest.approx(7.0)


def test_ema_insufficient_history_is_all_nan_same_length():
    s = pd.Series([1.0, 2.0, 3.0])
    out = ind.ema(s, span=5)
    assert len(out) == 3 and out.isna().all()


# --------------------------------------------------------------------------- rsi
def test_rsi_all_gains_is_100():
    s = pd.Series([float(i) for i in range(1, 30)])  # strictly increasing
    assert ind._last_valid(ind.rsi(s, period=14)) == pytest.approx(100.0)


def test_rsi_all_losses_is_0():
    s = pd.Series([float(i) for i in range(30, 1, -1)])  # strictly decreasing
    assert ind._last_valid(ind.rsi(s, period=14)) == pytest.approx(0.0)


def test_rsi_insufficient_history_is_all_nan():
    s = pd.Series([1.0, 2.0, 3.0])
    out = ind.rsi(s, period=14)
    assert len(out) == 3 and out.isna().all()


# --------------------------------------------------------------------------- atr
def test_atr_known_true_range():
    # Every bar: high=11, low=9, close=10 -> TR = max(11-9, |11-10|, |9-10|) = 2 -> ATR -> 2.0
    n = 20
    bars = pd.DataFrame({"high": [11.0] * n, "low": [9.0] * n, "close": [10.0] * n})
    assert ind._last_valid(ind.atr(bars, period=14)) == pytest.approx(2.0)


def test_atr_insufficient_history_is_all_nan():
    bars = pd.DataFrame({"high": [11.0, 11.0], "low": [9.0, 9.0], "close": [10.0, 10.0]})
    out = ind.atr(bars, period=14)
    assert len(out) == 2 and out.isna().all()


# --------------------------------------------------------------------------- determine_trend
def test_determine_trend_bullish_bearish_neutral():
    assert ind.determine_trend(110.0, 105.0, 103.0, 101.0) == "Bullish"
    assert ind.determine_trend(90.0, 95.0, 97.0, 99.0) == "Bearish"
    assert ind.determine_trend(100.0, 101.0, 99.0, 100.5) == "Neutral"  # not monotonic


def test_determine_trend_none_or_nan_is_neutral():
    assert ind.determine_trend(100.0, None, 99.0, 98.0) == "Neutral"
    assert ind.determine_trend(100.0, float("nan"), 99.0, 98.0) == "Neutral"


# --------------------------------------------------------------------------- _last_valid
def test_last_valid_skips_trailing_nan():
    s = pd.Series([1.0, 2.0, 3.0, np.nan, np.nan])
    assert ind._last_valid(s) == pytest.approx(3.0)


def test_last_valid_all_nan_is_none():
    assert ind._last_valid(pd.Series([np.nan, np.nan])) is None


def test_last_valid_empty_is_none():
    assert ind._last_valid(pd.Series([], dtype=float)) is None
