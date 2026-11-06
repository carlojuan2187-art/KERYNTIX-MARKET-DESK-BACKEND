from __future__ import annotations

from datetime import datetime, timezone

import pandas as pd

from engine import breakout_reset_strategy as breakout
from engine import liquidity_sweep_strategy as liquidity
from engine.strategy_router import select_strategy_name
from engine.dual_strategy_adapter import prepare_candidate


def test_both_engines_expose_the_hades_plus_filter_profile_only():
    assert breakout.PLUS_PROFILE["adx_min"] == 15.8
    assert breakout.PLUS_PROFILE["atr_pct_min"] == 0.0018
    assert breakout.PLUS_PROFILE["atr_pct_max"] == 0.0148
    assert breakout.PLUS_PROFILE["min_body_ratio_breakout"] == 0.23
    assert breakout.PLUS_PROFILE["min_body_ratio_continuation"] == 0.15
    assert breakout.PLUS_PROFILE["min_extension_atr"] == 0.16
    assert breakout.PLUS_PROFILE["max_extension_atr"] == 0.94
    assert breakout.PLUS_PROFILE["min_breakout_overshoot_atr"] == 0.08
    assert breakout.PLUS_PROFILE["min_pre_reset_space_atr"] == 0.06
    assert breakout.PLUS_PROFILE["min_rel_volume_continuation"] == 0.92
    assert breakout.PLUS_PROFILE["min_close_position_continuation"] == 0.53
    assert breakout.PLUS_PROFILE["min_post_breakout_progress_atr"] == 0.04
    assert liquidity.PROFILES == [liquidity.PLUS_PROFILE]
    assert liquidity.PLUS_PROFILE["min_sweep_atr"] == 0.10
    assert liquidity.PLUS_PROFILE["min_confirm_rel_volume"] == 0.82
    assert liquidity.PLUS_PROFILE["min_close_position"] == 0.53
    assert liquidity.PLUS_RAW_SCORE_MIN == 76.0
    assert breakout.PLUS_RAW_SCORE_MIN == 76.0


def test_market_regime_routes_continuation_and_sweep_states_to_distinct_engines():
    assert select_strategy_name({"state": "continuation_clean"}) == "breakout_reset"
    assert select_strategy_name({"state": "sweep_reversal"}) == "liquidity_sweep_reversal"
    assert select_strategy_name({"state": "vol_shock"}) == "liquidity_sweep_reversal"
    assert select_strategy_name({"state": "unknown"}) == "breakout_reset"


def test_adapter_creates_hades_risk_based_entry_zone_and_confirmation_time():
    df = pd.DataFrame({
        "close_time": [pd.Timestamp("2026-11-06T12:00:00Z"), pd.Timestamp("2026-11-06T12:05:00Z")],
        "open": [99.0, 100.0], "high": [101.0, 102.0],
        "low": [98.0, 99.0], "close": [100.0, 101.0], "volume": [1000.0, 1100.0],
    })
    candidate = {
        "strategy_name": "breakout_reset", "direction": "LONG", "entry_price": 100.0,
        "stop_loss": 99.0, "take_profits": [101.0, 102.0], "score_profile": "plus",
    }
    prepared = prepare_candidate(candidate, df_1h=df, df_15m=df, df_5m=df)
    assert prepared is not None
    assert prepared["setup_group"] == "plus"
    assert prepared["entry_zone"]["low"] < 100.0 < prepared["entry_zone"]["high"]
    assert prepared["setup_confirmed_at"] == datetime(2026, 11, 6, 12, 0, tzinfo=timezone.utc)


def test_adapter_rejects_invalid_trade_geometry():
    df = pd.DataFrame({"close_time": [pd.Timestamp("2026-11-06T12:00:00Z")], "open": [1.0], "high": [1.1], "low": [0.9], "close": [1.0], "volume": [100.0]})
    candidate = {"strategy_name": "breakout_reset", "direction": "LONG", "entry_price": 100, "stop_loss": 101, "take_profits": [102]}
    assert prepare_candidate(candidate, df_1h=df, df_15m=df, df_5m=df) is None
