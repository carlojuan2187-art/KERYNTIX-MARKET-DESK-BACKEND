import pytest

from engine.liquidity_strategy import (
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
    assert active["name"] == "premium"  # delivery tier remains unchanged
    assert _profile_name(active) == "plus"  # filter calibration is PLUS
    assert active["filter_profile"] == "plus"


def test_plus_base_filter_values_include_moderate_confirmation_tightening():
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
        assert MTF_PREMIUM_PROFILE[key] == value
    assert PLUS_RAW_SCORE_MIN == 76.0


def test_plus_long_adjustments_match_reference_strategy_effective_values():
    long = _directional_profile(MTF_PREMIUM_PROFILE, "LONG")
    short = _directional_profile(MTF_PREMIUM_PROFILE, "SHORT")

    assert long["min_rel_volume"] == pytest.approx(0.76)
    assert short["min_rel_volume"] == pytest.approx(0.76)
    assert long["min_sweep_atr"] == pytest.approx(0.15)
    assert long["min_confirm_rel_volume"] == pytest.approx(0.91)
    assert long["min_body_ratio_confirmation"] == pytest.approx(0.20)
    assert long["min_close_position"] == pytest.approx(0.60)
    assert long["min_rr"] == pytest.approx(1.02)
    assert long["htf_required_score"] == 2
    assert round(long["htf_price_tolerance"], 6) == 0.01012
    assert round(long["htf_trend_tolerance"], 6) == 0.0184
    assert _directional_min_raw_score("plus", "LONG") == 78.0

    assert short["min_sweep_atr"] == pytest.approx(0.10)
    assert short["min_confirm_rel_volume"] == pytest.approx(0.84)
    assert short["min_body_ratio_confirmation"] == pytest.approx(0.15)
    assert short["min_close_position"] == pytest.approx(0.55)
    assert short["min_rr"] == pytest.approx(0.90)
    assert short["htf_required_score"] == 1
    assert _directional_min_raw_score("plus", "SHORT") == 76.0
