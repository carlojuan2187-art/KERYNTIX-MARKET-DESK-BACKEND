from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from engine.liquidity_strategy import (
    LIQUIDITY_LOOKBACK,
    MIN_PUBLISH_TP1_RR,
    MTF_MIN_RAW_SCORE,
    MTF_PREMIUM_PROFILE,
    _find_recent_sweep,
    _higher_timeframe_context_ok,
    _select_liquidity_zone,
)


@pytest.mark.parametrize(
    "direction, no_breach, failed_reclaim, valid",
    [
        (
            "LONG",
            {"low": 100.01, "high": 101.0, "close": 100.1, "lower_wick": 0.6, "upper_wick": 0.05},
            {"low": 99.8, "high": 101.0, "close": 99.95, "lower_wick": 0.4, "upper_wick": 0.05},
            {"low": 99.8, "high": 101.0, "close": 100.1, "lower_wick": 0.4, "upper_wick": 0.05},
        ),
        (
            "SHORT",
            {"low": 99.0, "high": 99.99, "close": 99.9, "lower_wick": 0.05, "upper_wick": 0.6},
            {"low": 99.0, "high": 100.2, "close": 100.05, "lower_wick": 0.05, "upper_wick": 0.4},
            {"low": 99.0, "high": 100.2, "close": 99.9, "lower_wick": 0.05, "upper_wick": 0.4},
        ),
    ],
)
def test_liquidity_sweep_must_breach_pool_and_close_back_inside(direction, no_breach, failed_reclaim, valid):
    profile = {"min_sweep_atr": 0.10}

    def frame(last):
        if direction == "LONG":
            base = [
                {"low": 100.2, "high": 101.0, "close": 100.5, "lower_wick": 0.1, "upper_wick": 0.1},
                {"low": 100.3, "high": 101.1, "close": 100.6, "lower_wick": 0.1, "upper_wick": 0.1},
                {"low": 100.25, "high": 101.05, "close": 100.55, "lower_wick": 0.1, "upper_wick": 0.1},
            ]
        else:
            base = [
                {"low": 99.0, "high": 99.8, "close": 99.5, "lower_wick": 0.1, "upper_wick": 0.1},
                {"low": 98.9, "high": 99.7, "close": 99.4, "lower_wick": 0.1, "upper_wick": 0.1},
                {"low": 98.95, "high": 99.75, "close": 99.45, "lower_wick": 0.1, "upper_wick": 0.1},
            ]
        rows = base + [dict(last)]
        for row in rows:
            row["atr"] = 1.0
        return pd.DataFrame(rows)

    assert _find_recent_sweep(frame(no_breach), direction, 100.0, profile) is None
    assert _find_recent_sweep(frame(failed_reclaim), direction, 100.0, profile) is None
    sweep = _find_recent_sweep(frame(valid), direction, 100.0, profile)
    assert sweep is not None
    assert sweep["sweep_mode"] == "hard"
    assert sweep["sweep_distance_atr"] >= 0.10


def test_no_synthetic_liquidity_zone_is_created_without_a_confirmed_pivot():
    size = LIQUIDITY_LOOKBACK + 12
    low = np.linspace(110.0, 90.0, size)
    frame = pd.DataFrame({"low": low, "high": low + 1.0, "atr": np.ones(size)})
    assert _select_liquidity_zone(frame, "LONG", MTF_PREMIUM_PROFILE) is None


def test_higher_timeframe_context_fails_closed_and_rejects_opposite_trend():
    short_history = pd.DataFrame({
        "open": [100.0] * 30,
        "high": [101.0] * 30,
        "low": [99.0] * 30,
        "close": [100.0] * 30,
        "volume": [1000.0] * 30,
    })
    assert not _higher_timeframe_context_ok(short_history, "LONG", MTF_PREMIUM_PROFILE)

    close = np.linspace(100.0, 150.0, 120)
    rising = pd.DataFrame({
        "open": close - 0.1,
        "high": close + 0.5,
        "low": close - 0.5,
        "close": close,
        "volume": np.full(120, 1000.0),
    })
    assert _higher_timeframe_context_ok(rising, "LONG", MTF_PREMIUM_PROFILE)
    assert not _higher_timeframe_context_ok(rising, "SHORT", MTF_PREMIUM_PROFILE)


def test_active_signal_quality_thresholds_are_stricter_without_changing_r_math():
    assert MTF_MIN_RAW_SCORE == pytest.approx(82.0)
    assert MIN_PUBLISH_TP1_RR == pytest.approx(1.0)
