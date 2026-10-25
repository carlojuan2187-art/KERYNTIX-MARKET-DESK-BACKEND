from datetime import datetime, timedelta

from engine.outcome_logic import STATE_CLOSED, STATE_OPEN, STATE_TP1_HIT, advance_trade, build_result_doc, trade_levels

T0 = datetime(2026, 10, 23, 12, 0, 0)


def long_signal(tps=(101.5, 103.0)):
    return {
        "_id": "mongo-id", "id": "signal-uuid", "symbol": "BTCUSDT", "direction": "LONG", "entry_price": 100.1,
        "entry_zone": {"low": 100.0, "high": 100.2}, "stop_loss": 99.0,
        "take_profits": list(tps), "touched_at": T0, "created_at": T0 - timedelta(minutes=5),
        "outcome": {"state": STATE_OPEN, "tracked_until": T0, "tp1_at": None},
    }


def candle(minute, high, low):
    opened = T0 + timedelta(minutes=minute)
    return {"open_time": opened, "close_time": opened + timedelta(seconds=59, milliseconds=999), "high": high, "low": low}


def run(signal, candles, now=None):
    return advance_trade(signal, candles, now=now or T0 + timedelta(hours=5))


def test_real_r_is_calculated_from_modeled_fill():
    levels = trade_levels(long_signal())
    assert levels["fill"] == 100.2
    assert round(levels["r_tp1"], 4) == round(1.3 / 1.2, 4)
    assert round(levels["r_tp2"], 4) == round(2.8 / 1.2, 4)


def test_sl_first_is_minus_one_r():
    result = run(long_signal(), [candle(0, 100.5, 98.9)])
    assert result["closed"]["result"] == "SL"
    assert result["closed"]["r_real"] == -1.0
    assert not result["closed"]["ambiguous"]


def test_tp1_then_tp2_closes_at_tp2_multiple():
    result = run(long_signal(), [candle(0, 101.6, 100.3), candle(1, 103.2, 101.0)])
    assert result["closed"]["result"] == "TP2"
    assert round(result["closed"]["r_real"], 4) == round(2.8 / 1.2, 4)
    assert result["outcome"]["state"] == STATE_CLOSED


def test_tp1_then_sl_remains_tp1_win():
    result = run(long_signal(), [candle(0, 101.6, 100.3), candle(1, 100.9, 98.8)])
    assert result["closed"]["result"] == "TP1"
    assert result["closed"]["outcome"] == "win"
    assert result["events"][-1]["via"] == "SL_AFTER_TP1"


def test_tp1_only_updates_state_without_closing_when_tp2_exists():
    result = run(long_signal(), [candle(0, 101.6, 100.3)])
    assert result["closed"] is None
    assert result["outcome"]["state"] == STATE_TP1_HIT
    assert result["events"][0]["type"] == "TP1_HIT"


def test_single_target_closes_immediately_at_tp1():
    result = run(long_signal(tps=(101.5,)), [candle(0, 101.6, 100.3)])
    assert result["closed"]["result"] == "TP1"


def test_same_candle_sl_and_tp1_is_conservative_and_ambiguous():
    result = run(long_signal(), [candle(0, 101.6, 98.9)])
    assert result["closed"]["result"] == "SL"
    assert result["closed"]["ambiguous"] is True


def test_forming_candle_is_not_evaluated():
    now = T0 + timedelta(seconds=90)
    result = run(long_signal(), [candle(0, 100.5, 100.3), candle(1, 103.5, 98.0)], now=now)
    assert result["closed"] is None
    assert result["outcome"]["state"] == STATE_OPEN


def test_invalid_geometry_closes_invalid_without_false_trade_result():
    signal = long_signal()
    signal["stop_loss"] = 101.0
    result = run(signal, [candle(0, 100.5, 100.3)])
    assert result["outcome"]["result"] == "INVALID"
    assert result["closed"] is None


def test_result_document_uses_web_canonical_id():
    result = run(long_signal(), [candle(0, 101.6, 100.3), candle(1, 103.2, 101.0)])
    doc = build_result_doc(long_signal(), result["levels"], result["closed"])
    assert doc["signal_id"] == "signal-uuid"
    assert doc["id"] == "signal-uuid"
    assert doc["result"] == "TP2"
    assert doc["take_profits"] == [101.5, 103.0]
