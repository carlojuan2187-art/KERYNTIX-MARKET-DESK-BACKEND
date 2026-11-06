import pytest

from engine.liquidity_strategy import (
    MTF_MIN_RAW_SCORE,
    MTF_PREMIUM_PROFILE,
    PLUS_PROFILE,
    PLUS_RAW_SCORE_MIN,
    PROFILES,
    _directional_min_raw_score,
    _directional_profile,
    _profile_name,
)


def test_active_keryntix_signal_uses_reference_plus_filter_profile():
    assert len(PROFILES) == 1
    active = PROFILES[0]
    assert active["name"] == "premium"
    assert _profile_name(active) == "plus"
    assert active["filter_profile"] == "plus"


def test_plus_reference_values_are_preserved_and_live_tier_is_stricter():
    expected = {
        "atr_pct_min": 0.0015,
        "atr_pct_max": 0.0240,
        "liquidity_tolerance_atr": 0.50,
        "min_sweep_atr": 0.10,
        "min_rel_volume": 0.76,
        "min_confirm_rel_volume": 0.84,
        "min_body_ratio_recovery": 0.17,
        "min_body_ratio_confirmation": 0.15,
        "min_close_position": 0.55,
        "min_rr": 0.90,
        "htf_price_tolerance": 0.0110,
        "htf_trend_tolerance": 0.0200,
        "htf_required_score": 1,
    }
    for key, value in expected.items():
        assert PLUS_PROFILE[key] == value

    assert MTF_PREMIUM_PROFILE["atr_pct_min"] == expected["atr_pct_min"]
    assert MTF_PREMIUM_PROFILE["atr_pct_max"] == expected["atr_pct_max"]
    assert MTF_PREMIUM_PROFILE["liquidity_tolerance_atr"] == expected["liquidity_tolerance_atr"]
    assert MTF_PREMIUM_PROFILE["min_sweep_atr"] == expected["min_sweep_atr"]
    assert MTF_PREMIUM_PROFILE["min_confirm_rel_volume"] == expected["min_confirm_rel_volume"]
    assert MTF_PREMIUM_PROFILE["htf_price_tolerance"] == 0.006
    assert MTF_PREMIUM_PROFILE["htf_trend_tolerance"] == 0.010
    assert MTF_PREMIUM_PROFILE["htf_required_score"] == 3
    assert PLUS_RAW_SCORE_MIN == 76.0
    assert MTF_MIN_RAW_SCORE == 82.0


def test_directional_thresholds_are_mirrored_for_long_and_short():
    long_profile = _directional_profile(MTF_PREMIUM_PROFILE, "LONG")
    short_profile = _directional_profile(MTF_PREMIUM_PROFILE, "SHORT")

    assert long_profile == short_profile
    assert long_profile["min_sweep_atr"] == pytest.approx(0.10)
    assert long_profile["min_confirm_rel_volume"] == pytest.approx(0.84)
    assert long_profile["min_body_ratio_confirmation"] == pytest.approx(0.15)
    assert long_profile["min_close_position"] == pytest.approx(0.55)
    assert long_profile["min_rr"] == pytest.approx(0.90)
    assert long_profile["htf_required_score"] == 3
    assert long_profile["htf_price_tolerance"] == pytest.approx(0.006)
    assert long_profile["htf_trend_tolerance"] == pytest.approx(0.010)
    assert _directional_min_raw_score("plus", "LONG") == 76.0
    assert _directional_min_raw_score("plus", "SHORT") == 76.0
