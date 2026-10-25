from datetime import datetime, timedelta, timezone

from engine.lifecycle_logic import (
    STATUS_CANCELLED, STATUS_TOUCHED, CANCEL_AMBIGUOUS_INTRABAR,
    CANCEL_ENTRY_NOT_TOUCHED, CANCEL_TP_REACHED_BEFORE_ENTRY,
    current_price_event, evaluate_pending_signal, transition_update,
)

T0 = datetime(2026, 10, 23, 14, 0, 0)


def long_signal():
    return {
        "id": "long-1", "direction": "LONG", "symbol": "BTCUSDT",
        "entry_price": 100.5, "entry_zone": {"low": 100.0, "high": 101.0},
        "stop_loss": 98.0, "take_profits": [110.0, 115.0], "atr_value": 10.0,
        "created_at": T0, "entry_expires_at": T0 + timedelta(minutes=15),
        "status": "PENDING",
    }


def short_signal():
    return {
        "id": "short-1", "direction": "SHORT", "symbol": "ETHUSDT",
        "entry_price": 109.5, "entry_zone": {"low": 109.0, "high": 110.0},
        "stop_loss": 112.0, "take_profits": [100.0, 95.0], "atr_value": 10.0,
        "created_at": T0, "entry_expires_at": T0 + timedelta(minutes=15),
        "status": "PENDING",
    }


def test_live_long_and_short_prices_touch_zone():
    assert current_price_event(long_signal(), 100.8)["status"] == STATUS_TOUCHED
    assert current_price_event(short_signal(), 109.2)["status"] == STATUS_TOUCHED


def test_tp_before_entry_cancels_long():
    event = current_price_event(long_signal(), 110.2)
    assert event["status"] == STATUS_CANCELLED
    assert event["reason"] == CANCEL_TP_REACHED_BEFORE_ENTRY


def test_expiry_cancels_when_entry_never_touched():
    signal = long_signal()
    event = evaluate_pending_signal(signal, [], now=signal["entry_expires_at"] + timedelta(seconds=1), current_price=105.0)
    assert event["status"] == STATUS_CANCELLED
    assert event["reason"] == CANCEL_ENTRY_NOT_TOUCHED


def test_same_candle_entry_and_tp_is_ambiguous_and_cancelled():
    signal = long_signal()
    candle = {"open_time": T0 + timedelta(minutes=1), "close_time": T0 + timedelta(minutes=2), "low": 100.5, "high": 111.0}
    event = evaluate_pending_signal(signal, [candle], now=candle["close_time"], current_price=105.0)
    assert event["status"] == STATUS_CANCELLED
    assert event["reason"] == CANCEL_AMBIGUOUS_INTRABAR


def test_candle_touch_returns_zone_edge_as_the_fill_reference():
    signal = long_signal()
    candle = {"open_time": T0 + timedelta(minutes=1), "close_time": T0 + timedelta(minutes=2), "low": 100.5, "high": 102.0}
    event = evaluate_pending_signal(signal, [candle], now=candle["close_time"], current_price=100.8)
    assert event["status"] == STATUS_TOUCHED
    assert event["event_price"] == 101.0


def test_transition_update_materializes_flat_and_nested_lifecycle():
    fields = transition_update(long_signal(), {"status": STATUS_TOUCHED, "reason": "ENTRY_ZONE_TOUCHED", "event_at": T0, "event_price": 100.8})
    assert fields["status"] == "TOUCHED"
    assert fields["lifecycle.status"] == "TOUCHED"
    assert fields["touch_price"] == 100.8
    assert fields["outcome"]["state"] == "OPEN"
    assert fields["outcome"]["tracked_until"] == T0
