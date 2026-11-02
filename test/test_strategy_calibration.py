from datetime import datetime, timezone

import pandas as pd

from engine.liquidity_strategy import (
    MTF_PREMIUM_PROFILE,
    _directional_context_ok,
    _directional_profile,
    _score_components,
)


def _context_frame(*, close: float, previous_close: float, high: float, previous_high: float,
                   low: float, previous_low: float, ema20: float, previous_ema20: float,
                   rel_volume: float) -> pd.DataFrame:
    return pd.DataFrame([
        {"close": previous_close, "high": previous_high, "low": previous_low,
         "ema20": previous_ema20, "rel_volume": 0.2},
        {"close": close, "high": high, "low": low,
         "ema20": ema20, "rel_volume": rel_volume},
    ])


def test_directional_thresholds_are_shared_for_long_and_short():
    long_profile = _directional_profile(MTF_PREMIUM_PROFILE, "LONG")
    short_profile = _directional_profile(MTF_PREMIUM_PROFILE, "SHORT")

    assert long_profile == short_profile
    assert long_profile["min_sweep_atr"] == MTF_PREMIUM_PROFILE["min_sweep_atr"]
    assert long_profile["min_rr"] == MTF_PREMIUM_PROFILE["min_rr"]


def test_directional_context_requires_two_of_four_for_both_sides():
    profile = {"filter_profile": "plus", "min_confirm_rel_volume": 0.82}

    long_one_check = _context_frame(
        close=100.5, previous_close=101.0, high=101.0, previous_high=102.0,
        low=100.0, previous_low=100.5, ema20=100.0, previous_ema20=100.2,
        rel_volume=0.2,
    )
    short_one_check = _context_frame(
        close=99.5, previous_close=99.0, high=100.0, previous_high=100.5,
        low=98.0, previous_low=97.5, ema20=100.0, previous_ema20=99.8,
        rel_volume=0.2,
    )
    assert not _directional_context_ok(long_one_check, "LONG", profile)
    assert not _directional_context_ok(short_one_check, "SHORT", profile)

    long_two_checks = _context_frame(
        close=100.5, previous_close=101.0, high=101.0, previous_high=102.0,
        low=100.0, previous_low=100.5, ema20=100.0, previous_ema20=100.2,
        rel_volume=0.9,
    )
    short_two_checks = _context_frame(
        close=99.5, previous_close=99.0, high=100.0, previous_high=100.5,
        low=98.0, previous_low=97.5, ema20=100.0, previous_ema20=99.8,
        rel_volume=0.9,
    )
    assert _directional_context_ok(long_two_checks, "LONG", profile)
    assert _directional_context_ok(short_two_checks, "SHORT", profile)


def test_ema_score_rewards_real_15m_stack_and_partial_reclaim():
    trigger = pd.Series({
        "close": 101.0, "rel_volume": 1.0, "body_ratio": 0.5,
        "close_position": 0.8,
    })
    profile = {
        "min_confirm_rel_volume": 0.8,
        "min_body_ratio_recovery": 0.15,
        "min_close_position": 0.5,
        "min_sweep_atr": 0.1,
        "min_rr": 0.9,
    }
    zone = {"count": 2}
    sweep = {"sweep_distance_atr": 0.2}

    partial = _score_components(
        trigger, "LONG", zone, sweep, 1.2, profile,
        trend_reference=pd.Series({"close": 101.0, "ema20": 100.0, "ema50": 102.0}),
    )
    aligned = _score_components(
        trigger, "LONG", zone, sweep, 1.2, profile,
        trend_reference=pd.Series({"close": 101.0, "ema20": 100.0, "ema50": 99.0}),
    )

    partial_ema = next(item for item in partial if item["name"] == "ema_reclaim")
    aligned_ema = next(item for item in aligned if item["name"] == "ema_reclaim")
    assert partial_ema["points"] == 5.0
    assert aligned_ema["points"] == 10.0
