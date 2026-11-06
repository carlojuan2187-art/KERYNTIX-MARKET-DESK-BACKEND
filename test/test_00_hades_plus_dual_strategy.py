from __future__ import annotations

from datetime import datetime, timedelta, timezone
import importlib
import inspect

import pandas as pd

from engine import breakout_reset_strategy as breakout
from engine.outcome_logic import build_result_doc
from engine import liquidity_sweep_strategy as liquidity
from engine.dual_strategy_adapter import prepare_candidate
from engine.prepublication import validate_prepublication_setup
from engine.strategy_router import select_strategy_name
from worker.signal_scanner import SignalScanner, _latest_sl_by_symbol_direction


def test_both_hades_engines_expose_plus_profiles_only():
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


def test_btc_market_regime_selects_distinct_engines_and_unknown_fails_open():
    assert select_strategy_name({"state": "continuation_clean"}) == "breakout_reset"
    assert select_strategy_name({"state": "sweep_reversal"}) == "liquidity_sweep_reversal"
    assert select_strategy_name({"state": "vol_shock"}) == "liquidity_sweep_reversal"
    assert select_strategy_name({"state": "unknown"}) == "breakout_reset"


def test_dual_strategy_scanner_uses_regime_router_and_full_history():
    source = inspect.getsource(SignalScanner.scan_once)
    assert "classify_market_regime" in source
    assert "route_candidate(" in source
    assert "prepare_candidate(" in source
    assert '"5m", 320' in source
    assert '"1h", 240' in source
    assert '"15m", 240' in source


def test_result_reentry_guard_keeps_strategy_stops_independent():
    lsr_stop = datetime(2026, 11, 4, 10, 0, tzinfo=timezone.utc)
    breakout_stop = lsr_stop + timedelta(minutes=30)
    latest = _latest_sl_by_symbol_direction([
        {"symbol": "BTCUSDT", "direction": "LONG", "result": "SL",
         "strategy_name": "liquidity_sweep_reversal", "closed_at": lsr_stop},
        {"symbol": "BTCUSDT", "direction": "LONG", "result": "SL",
         "strategy_name": "breakout_reset", "closed_at": breakout_stop},
    ])
    assert latest[("BTCUSDT", "LONG")] == lsr_stop


def test_adapter_creates_risk_based_entry_zone_and_confirmation_time():
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


def test_hades_candidate_uses_its_native_entry_distance_gate():
    signal = {
        "strategy_name": "breakout_reset",
        "direction": "LONG",
        "entry_zone": {"low": 99.9, "high": 100.1},
        "entry_price": 100.0,
        "stop_loss": 99.0,
        "take_profits": [110.0],
        "atr_value": 1.0,
    }
    allowed, reason = validate_prepublication_setup(signal, current_price=102.0, candles=[])
    assert allowed is True
    assert reason is None


def test_adapter_rejects_invalid_trade_geometry():
    df = pd.DataFrame({
        "close_time": [pd.Timestamp("2026-11-06T12:00:00Z")],
        "open": [1.0], "high": [1.1], "low": [0.9], "close": [1.0], "volume": [100.0],
    })
    candidate = {
        "strategy_name": "breakout_reset", "direction": "LONG",
        "entry_price": 100, "stop_loss": 101, "take_profits": [102],
    }
    assert prepare_candidate(candidate, df_1h=df, df_15m=df, df_5m=df) is None


def test_closed_results_retain_strategy_and_regime_attribution():
    created = datetime(2026, 11, 6, 12, 0, tzinfo=timezone.utc)
    closed = datetime(2026, 11, 6, 12, 15, tzinfo=timezone.utc)
    snapshot = build_result_doc(
        {
            "id": "signal-1", "symbol": "BTCUSDT", "direction": "LONG",
            "visibility": "premium", "entry_price": 100.0,
            "entry_zone": {"low": 99.9, "high": 100.1}, "stop_loss": 99.0,
            "take_profits": [101.0, 102.0], "created_at": created,
            "strategy_name": "breakout_reset", "strategy_version": "v15",
            "router_version": "v3", "regime_state": "continuation_clean",
            "regime_bias": "up", "regime_reason": "market_regime_continuation_clean",
            "regime_strategy_selected": "breakout_reset",
            "router_primary_strategy": "breakout_reset", "router_fallback_used": False,
        },
        {
            "direction": "LONG", "fill": 100.0, "entry_reference": "entry_price",
            "sl": 99.0, "tp1": 101.0, "tp2": 102.0, "risk": 1.0,
            "r_tp1": 1.0, "r_tp2": 2.0,
        },
        {
            "result": "TP1", "outcome": "win", "r_real": 1.0, "ambiguous": False,
            "closed_at": closed, "tp1_at": closed, "tp2_at": None,
            "via": "TP1", "calculation_version": "2.0", "execution_model": "test",
        },
    )
    assert snapshot["strategy_name"] == "breakout_reset"
    assert snapshot["regime_state"] == "continuation_clean"
    assert snapshot["router_fallback_used"] is False


def test_hades_lsr_ignores_legacy_keryntix_environment_overrides(monkeypatch):
    monkeypatch.setenv("LSR_PLUS_MIN_CONFIRM_REL_VOLUME", "0.84")
    monkeypatch.setenv("LSR_PLUS_MIN_CLOSE_POSITION", "0.55")
    monkeypatch.setenv("LSR_PULLBACK_RETRACE_FRACTION", "0.42")
    monkeypatch.setenv("LSR_PULLBACK_ATR_MIN", "0.24")
    for name in (
        "HADES_LSR_PLUS_MIN_CONFIRM_REL_VOLUME", "HADES_LSR_PLUS_MIN_CLOSE_POSITION",
        "HADES_LSR_PULLBACK_RETRACE_FRACTION", "HADES_LSR_PULLBACK_ATR_MIN",
    ):
        monkeypatch.delenv(name, raising=False)
    isolated = importlib.reload(liquidity)
    assert isolated.PLUS_PROFILE["min_confirm_rel_volume"] == 0.82
    assert isolated.PLUS_PROFILE["min_close_position"] == 0.53
    assert isolated.PULLBACK_RETRACE_FRACTION == 0.32
    assert isolated.PULLBACK_ATR_MIN == 0.12
