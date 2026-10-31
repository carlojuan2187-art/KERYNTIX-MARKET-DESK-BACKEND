from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from domain.signals import (
    CanonicalSignal,
    CanonicalSignalResult,
    SignalStatus,
    adapt_bot_result,
    adapt_bot_signal,
    compute_trade_levels,
    lifecycle_geometry_issues,
)


def legacy_signal(**overrides):
    base = {
        "_id": "mongo-signal-001",
        "symbol": "btcusdt",
        "direction": "LONG",
        "visibility": "premium",
        "entry_price": 100.0,
        "entry_zone": {"low": 99.5, "high": 100.5},
        "stop_loss": 98.0,
        "take_profits": [105.5, 110.5],
        "timeframes": ["15M", "5M"],
        "score": 86.0,
        "components": [
            {"name": "liquidity_zone", "points": 18.0, "value": 2.0, "passed": True},
            {"name": "minimum_sweep", "points": 18.0, "value": 0.8, "passed": True},
        ],
        "raw_score": 86.0,
        "normalized_score": 86.0,
        "raw_components": [
            {"name": "liquidity_zone", "points": 18.0, "value": 2.0, "passed": True}
        ],
        "normalized_components": [
            {"name": "liquidity_zone", "points": 18.0, "value": 2.0, "passed": True}
        ],
        "score_profile": "premium",
        "score_calibration": "v3",
        "setup_group": "liquidity_sweep",
        "delivery_plan": "premium",
        "filter_profile": "premium",
        "setup_stage": "waiting_pullback",
        "send_mode": "pending_entry",
        "entry_model": "liquidity_pullback",
        "entry_model_price": 100.1,
        "entry_sent_price": 101.2,
        "execution_timeframe": "5M",
        "setup_confirmed_at": datetime(2026, 10, 25, 12, 0),
        "atr_pct": 0.8,
        "atr_value": 2.5,
        "risk_atr": 1.0,
        "target_model": "structural_targets",
        "tp1_rr": 2.0,
        "preentry_distance_atr": 0.5,
        "bars_after_sweep": 2,
        "sweep_to_market_atr": 0.7,
        "liquidity_zone": 98.9,
        "liquidity_sweep_distance_atr": 0.6,
        "liquidity_sweep_extreme_price": 98.4,
        "higher_tf_context": {"liquidity_zone": 98.9, "sweep_distance_atr": 0.6},
        "strategy_runtime": {"post_fill_invalidation": {"minutes": 10, "reason": "no_followthrough"}},
        "created_at": datetime(2026, 10, 25, 12, 1),
        "valid_until": datetime(2026, 10, 25, 12, 16),
        "telegram_valid_until": datetime(2026, 10, 25, 12, 16),
        "entry_expires_at": datetime(2026, 10, 25, 12, 16),
        "display_until": datetime(2026, 10, 25, 12, 16),
        "estimated_entry_minutes": {"min": 1, "max": 5},
        "status": "TOUCHED",
        "status_updated_at": datetime(2026, 10, 25, 12, 2),
        "pending_at": datetime(2026, 10, 25, 12, 1),
        "touched_at": datetime(2026, 10, 25, 12, 2),
        "touch_price": 100.5,
        "cancelled_at": None,
        "cancel_reason": None,
        "outcome": {"state": "OPEN", "result": None, "tracked_until": datetime(2026, 10, 25, 12, 2)},
        "leverage": None,
    }
    base.update(overrides)
    return base


def test_adapter_preserves_engine_metadata_and_scoring():
    signal = adapt_bot_signal(legacy_signal())

    assert signal.id == "mongo-signal-001"
    assert signal.symbol == "BTCUSDT"
    assert signal.scoring.raw_score == 86
    assert signal.scoring.normalized_score == 86
    assert signal.scoring.profile == "premium"
    assert signal.scoring.calibration == "v3"
    assert signal.scoring.components[0].name == "liquidity_zone"
    assert signal.strategy.setup_group == "liquidity_sweep"
    assert signal.strategy.atr_pct == pytest.approx(0.8)
    assert signal.strategy.higher_tf_context["liquidity_zone"] == pytest.approx(98.9)
    assert signal.strategy.runtime.post_fill_invalidation.minutes == 10
    # The original bot's currently allow-listed document may not contain targets;
    # the adapter must not hallucinate them.
    assert signal.targets == []
    assert signal.trade_metrics.modeled_entry_fill == pytest.approx(100.0)


def test_legacy_naive_datetimes_are_normalized_as_utc():
    signal = adapt_bot_signal(legacy_signal())
    assert signal.lifecycle.created_at.tzinfo == timezone.utc
    assert signal.strategy.setup_confirmed_at.tzinfo == timezone.utc
    encoded = signal.model_dump(mode="json")
    assert encoded["lifecycle"]["created_at"].endswith("Z")
    assert encoded["strategy"]["setup_confirmed_at"].endswith("Z")


def test_touch_price_is_not_modeled_entry_fill():
    signal = adapt_bot_signal(legacy_signal())
    assert signal.lifecycle.touch_price == pytest.approx(100.5)
    assert signal.trade_metrics.modeled_entry_fill == pytest.approx(100.0)
    # Use different observed quote to explicitly ensure the fields are not aliases.
    signal = adapt_bot_signal(legacy_signal(touch_price=100.42))
    assert signal.lifecycle.touch_price == pytest.approx(100.42)
    assert signal.trade_metrics.modeled_entry_fill == pytest.approx(100.0)


def test_pending_signal_does_not_report_fill_metrics_as_realized():
    pending = legacy_signal(status="PENDING", touched_at=None, touch_price=None,
                            outcome={"state": None, "result": None})
    signal = adapt_bot_signal(pending)
    assert signal.lifecycle.status == SignalStatus.PENDING
    assert signal.trade_metrics is None


def test_inverted_legacy_entry_zone_is_normalized_like_bot_monitor():
    signal = adapt_bot_signal(legacy_signal(entry_zone={"low": 101.0, "high": 99.0}))
    assert signal.entry_zone.low == 99.0
    assert signal.entry_zone.high == 101.0


def test_missing_legacy_zone_uses_existing_bot_fallback_formula():
    signal = adapt_bot_signal(legacy_signal(entry_zone=None, status="PENDING", touched_at=None,
                                            outcome={"state": None, "result": None}))
    assert signal.entry_zone.low == round(100.0 * (1 - 0.0015), 4)
    assert signal.entry_zone.high == round(100.0 * (1 + 0.0015), 4)


def test_invalid_lifecycle_geometry_is_reported_not_silently_fixed():
    bad = adapt_bot_signal(legacy_signal(stop_loss=101.0))
    assert lifecycle_geometry_issues(bad) == ["STOP_LOSS_NOT_BELOW_ENTRY_ZONE"]
    assert bad.stop_loss == 101.0


def test_long_trade_levels_match_current_tracker_geometry_and_r():
    signal = adapt_bot_signal(legacy_signal())
    levels = compute_trade_levels(signal)
    assert levels is not None
    assert levels["fill"] == pytest.approx(100.0)  # canonical strategy entry, not zone edge
    assert levels["risk"] == pytest.approx(2.0)
    assert levels["r_tp1"] == pytest.approx(2.75)
    assert levels["r_tp2"] == pytest.approx(5.25)


def test_short_trade_levels_use_zone_low_and_reverse_target_order():
    raw = legacy_signal(
        direction="SHORT", entry_price=100.0,
        entry_zone={"low": 99.5, "high": 100.5}, stop_loss=102.0,
        take_profits=[94.5, 90.5], status="TOUCHED", touch_price=99.5,
        outcome={"state": "OPEN", "result": None},
    )
    signal = adapt_bot_signal(raw)
    levels = compute_trade_levels(signal)
    assert levels is not None
    assert levels["fill"] == pytest.approx(100.0)
    assert levels["risk"] == pytest.approx(2.0)
    assert levels["r_tp1"] == pytest.approx(2.75)
    assert levels["r_tp2"] == pytest.approx(4.75)
    assert lifecycle_geometry_issues(signal) == []


def test_tracker_uses_first_two_positive_targets_and_does_not_invent_tp3():
    signal = adapt_bot_signal(legacy_signal(take_profits=[105.5, 110.5, 120.0], targets=[]))
    levels = compute_trade_levels(signal)
    assert levels["tp1"] == pytest.approx(105.5)
    assert levels["tp2"] == pytest.approx(110.5)
    assert len(signal.take_profits) == 3  # contract preserves original list
    assert signal.trade_metrics.r_by_target_from_modeled_fill == pytest.approx([2.75, 5.25])


def test_bad_target_order_is_untrackable_not_reordered():
    signal = adapt_bot_signal(legacy_signal(take_profits=[105.5, 104.0]))
    assert compute_trade_levels(signal) is None
    assert signal.take_profits == [105.5, 104.0]


def test_schema_rejects_nonfinite_prices_and_bad_entry_zone():
    raw = legacy_signal()
    raw["entry_price"] = float("nan")
    with pytest.raises(ValidationError):
        adapt_bot_signal(raw)

    raw = legacy_signal(entry_zone={"low": 102.0, "high": 99.0})
    # The legacy adapter normalizes inverted zones, as the current monitor does.
    assert adapt_bot_signal(raw).entry_zone.low == 99.0


def test_outcome_cannot_be_closed_without_a_result():
    with pytest.raises(ValidationError):
        adapt_bot_signal(legacy_signal(outcome={"state": "CLOSED", "result": None}))


def test_result_adapter_preserves_tracked_result_and_utc_dates():
    raw = {
        "_id": "result-001", "signal_id": "mongo-signal-001", "symbol": "btcusdt",
        "direction": "LONG", "visibility": "premium", "entry_fill": 100.5,
        "entry_zone": {"low": 99.5, "high": 100.5}, "stop_loss": 98.0,
        "take_profits": [105.5, 110.5], "risk": 2.5, "r_tp1": 2.0,
        "r_tp2": 4.0, "result": "TP2", "outcome": "win", "r_real": 4.0,
        "ambiguous": False, "score": 86,
        "created_at": datetime(2026, 10, 25, 12, 1),
        "touched_at": datetime(2026, 10, 25, 12, 2),
        "tp1_at": datetime(2026, 10, 25, 12, 3),
        "closed_at": datetime(2026, 10, 25, 12, 4),
        "evaluated_at": datetime(2026, 10, 25, 12, 4), "duration_minutes": 2.0,
    }
    result = adapt_bot_result(raw)
    assert result.id == "result-001"
    assert result.result == "TP2"
    assert result.outcome == "win"
    assert result.r_real == pytest.approx(4.0)
    assert result.closed_at.tzinfo == timezone.utc


def test_result_adapter_rejects_win_loss_mismatch():
    raw = {
        "_id": "result-002", "signal_id": "sig-2", "symbol": "ETHUSDT",
        "direction": "SHORT", "entry_fill": 100, "entry_zone": {"low": 99, "high": 101},
        "stop_loss": 102, "take_profits": [98], "risk": 2, "r_tp1": 1,
        "result": "SL", "outcome": "win", "r_real": -1, "ambiguous": False,
        "closed_at": datetime.now(timezone.utc), "evaluated_at": datetime.now(timezone.utc),
    }
    with pytest.raises(ValidationError):
        adapt_bot_result(raw)


def test_result_adapter_preserves_v2_execution_and_tp2_metadata():
    raw = {
        "id": "result-v2", "signal_id": "signal-v2", "symbol": "BTCUSDT",
        "direction": "LONG", "visibility": "premium", "entry_fill": 100.0,
        "entry_zone": {"low": 99.5, "high": 100.5}, "stop_loss": 98.0,
        "take_profits": [105.5, 110.5], "risk": 2.0, "r_tp1": 2.75,
        "r_tp2": 5.25, "result": "TP2", "outcome": "win", "r_real": 5.25,
        "ambiguous": False, "score": 90,
        "created_at": datetime(2026, 10, 25, 12, 1),
        "touched_at": datetime(2026, 10, 25, 12, 2),
        "tp1_at": datetime(2026, 10, 25, 12, 3),
        "tp2_at": datetime(2026, 10, 25, 12, 4),
        "closed_at": datetime(2026, 10, 25, 12, 5),
        "evaluated_at": datetime(2026, 10, 25, 12, 5),
        "entry_reference": "entry_price", "exit_price": 110.5,
        "exit_via": "TP2_LIVE_PRICE", "calculation_version": "2.0",
        "execution_model": "SIMULATED_FULL_POSITION_TP1_PROTECTION",
        "duration_minutes": 3.0,
    }
    result = adapt_bot_result(raw)
    assert result.tp2_at == datetime(2026, 10, 25, 12, 4, tzinfo=timezone.utc)
    assert result.entry_reference == "entry_price"
    assert result.exit_price == pytest.approx(110.5)
    assert result.exit_via == "TP2_LIVE_PRICE"
    assert result.calculation_version == "2.0"
    assert result.execution_model == "SIMULATED_FULL_POSITION_TP1_PROTECTION"
