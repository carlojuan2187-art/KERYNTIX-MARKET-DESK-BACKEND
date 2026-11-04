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


def candle(minute, high, low, open_price=None):
    opened = T0 + timedelta(minutes=minute)
    return {
        "open_time": opened,
        "close_time": opened + timedelta(seconds=59, milliseconds=999),
        "open": open_price if open_price is not None else 100.1,
        "high": high,
        "low": low,
    }


def run(signal, candles, now=None):
    return advance_trade(signal, candles, now=now or T0 + timedelta(hours=5))


def test_real_r_is_calculated_from_modeled_fill():
    levels = trade_levels(long_signal())
    assert levels["fill"] == 100.1
    assert levels["entry_reference"] == "entry_price"
    assert round(levels["risk"], 4) == 1.1
    assert round(levels["r_tp1"], 4) == round(1.4 / 1.1, 4)
    assert round(levels["r_tp2"], 4) == round(2.9 / 1.1, 4)


def test_sl_first_is_minus_one_r():
    result = run(long_signal(), [candle(0, 100.5, 98.9)])
    assert result["closed"]["result"] == "SL"
    assert result["closed"]["r_real"] == -1.0
    assert not result["closed"]["ambiguous"]




def test_stop_r_uses_each_signals_dynamic_stop_and_gap_fill_price():
    signal = long_signal()
    signal["stop_loss"] = 99.6  # this trade's own SL; risk is 0.5, not a global fixed distance
    gap_candle = candle(0, 100.2, 99.2, open_price=99.4)  # opens beyond the stop
    result = run(signal, [gap_candle])

    assert result["closed"]["result"] == "SL"
    assert result["closed"]["exit_price"] == 99.4
    # Entry 100.1 -> observed gap fill 99.4 = -0.7; initial risk 0.5 => -1.4R.
    assert result["closed"]["r_real"] == -1.4
    assert round(result["levels"]["risk"], 4) == 0.5


def test_exact_dynamic_stop_fill_is_minus_one_r_by_definition():
    signal = long_signal()
    signal["stop_loss"] = 99.6
    result = run(signal, [candle(0, 100.2, 99.5, open_price=100.0)])

    assert result["closed"]["result"] == "SL"
    assert result["closed"]["exit_price"] == 99.6
    assert result["closed"]["r_real"] == -1.0
    assert round(result["levels"]["risk"], 4) == 0.5


def test_short_stop_gap_uses_the_dynamic_short_stop_and_actual_open():
    signal = long_signal()
    signal.update({
        "direction": "SHORT", "stop_loss": 100.6,
        "take_profits": [99.4, 98.8],
    })
    result = run(signal, [candle(0, 100.9, 100.2, open_price=100.8)])

    assert result["closed"]["result"] == "SL"
    assert result["closed"]["exit_price"] == 100.8
    # Entry 100.1 -> short exit 100.8 = -0.7; initial risk 0.5 => -1.4R.
    assert result["closed"]["r_real"] == -1.4


def test_tp1_then_tp2_closes_at_tp2_multiple():
    result = run(long_signal(), [candle(0, 101.6, 100.3), candle(1, 103.2, 101.6)])
    assert result["closed"]["result"] == "TP2"
    assert round(result["closed"]["r_real"], 4) == round(2.9 / 1.1, 4)
    assert result["closed"]["r_real"] != round((1.4 + 2.9) / 1.1, 4)
    assert result["outcome"]["state"] == STATE_CLOSED


def test_tp1_then_sl_remains_tp1_win():
    result = run(long_signal(), [candle(0, 101.6, 100.3), candle(1, 100.9, 98.8)])
    assert result["closed"]["result"] == "TP1"
    assert result["closed"]["outcome"] == "win"
    assert result["events"][-1]["via"] == "TP1_ENTRY_PROTECTION"
    assert result["closed"]["exit_price"] == 101.5
    assert result["closed"]["exit_trigger_price"] == 100.1
    assert result["closed"]["r_real"] == round(result["levels"]["r_tp1"], 4)


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
    result = run(long_signal(), [candle(0, 101.6, 100.3), candle(1, 103.2, 101.6)])
    doc = build_result_doc(long_signal(), result["levels"], result["closed"])
    assert doc["signal_id"] == "signal-uuid"
    assert doc["id"] == "signal-uuid"
    assert doc["result"] == "TP2"
    assert doc["take_profits"] == [101.5, 103.0]


def test_tp1_protection_closes_on_retest_without_touching_original_sl():
    signal = long_signal()
    result = run(signal, [
        candle(0, 101.6, 100.3),  # TP1 reached and protection armed at candle close
        candle(1, 101.8, 100.0),  # retest of entry; result retains TP1 R
    ])
    assert result["closed"]["result"] == "TP1"
    assert result["closed"]["exit_price"] == 101.5
    assert result["closed"]["exit_trigger_price"] == 100.1
    assert result["closed"]["via"] in {"TP1_ENTRY_PROTECTION", "TP1_ENTRY_RETEST_SAME_CANDLE_AMBIGUOUS"}
    assert result["closed"]["r_real"] == round(result["levels"]["r_tp1"], 4)


def test_tp1_protection_short_closes_on_retest():
    signal = long_signal()
    signal.update({
        "direction": "SHORT", "entry_price": 100.1,
        "entry_zone": {"low": 100.0, "high": 100.2},
        "stop_loss": 101.2, "take_profits": [98.7, 97.2],
    })
    first = {"open_time": T0, "close_time": T0 + timedelta(seconds=59), "high": 100.0, "low": 98.6}
    second = {"open_time": T0 + timedelta(minutes=1), "close_time": T0 + timedelta(minutes=1, seconds=59), "high": 100.2, "low": 98.0}
    result = run(signal, [first, second])
    assert result["closed"]["result"] == "TP1"
    assert result["closed"]["exit_price"] == 98.7
    assert result["closed"]["exit_trigger_price"] == 100.1
    assert result["closed"]["via"] in {"TP1_ENTRY_PROTECTION", "TP1_ENTRY_RETEST_SAME_CANDLE_AMBIGUOUS"}


def test_tp1_and_tp2_with_retest_in_same_candle_resolves_conservatively_at_tp1():
    signal = long_signal()
    result = run(signal, [
        candle(0, 101.6, 100.3),
        candle(1, 103.2, 99.9),
    ])
    assert result["closed"]["result"] == "TP1"
    assert result["closed"]["ambiguous"] is True
    assert result["closed"]["exit_price"] == 101.5


def test_live_price_advances_tp1_then_closes_on_live_retest():
    signal = long_signal()
    first_at = T0 + timedelta(seconds=5)
    first = advance_trade(signal, [], now=first_at, live_price=101.6, live_price_at=first_at)
    assert first["closed"] is None
    assert first["outcome"]["state"] == STATE_TP1_HIT
    assert first["outcome"]["protective_stop_price"] == 100.1

    signal["outcome"] = first["outcome"]
    second_at = T0 + timedelta(seconds=7)
    second = advance_trade(signal, [], now=second_at, live_price=100.1, live_price_at=second_at)
    assert second["closed"]["result"] == "TP1"
    assert second["closed"]["exit_price"] == 101.5
    assert second["closed"]["exit_trigger_price"] == 100.1
    assert second["closed"]["r_real"] == first["events"][0]["r_tp1"]


def test_result_snapshot_labels_simulated_model_and_separates_actual_zone_touch():
    signal = long_signal()
    signal["touch_price"] = 100.18
    step = run(signal, [candle(0, 101.6, 100.3), candle(1, 101.7, 100.0)])
    doc = build_result_doc(signal, step["levels"], step["closed"])

    assert doc["entry_fill"] == 100.1
    assert doc["entry_reference"] == "entry_price"
    assert doc["touch_price"] == 100.18
    assert doc["exit_price"] == 101.5
    assert doc["exit_via"] == "TP1_ENTRY_PROTECTION"
    assert doc["exit_trigger_price"] == 100.1
    assert doc["calculation_version"] == "2.3"
    assert doc["risk"] == step["levels"]["risk"]
    assert doc["risk_pct"] == round((step["levels"]["risk"] / step["levels"]["fill"]) * 100.0, 6)
    assert doc["execution_model"] == "SIMULATED_FULL_POSITION_TP1_CREDIT_ENTRY_PROTECTION"
    assert doc["r_real"] == step["closed"]["r_real"]


def test_tp1_tp2_and_return_to_tp1_in_same_candle_closes_conservatively_at_tp1():
    result = run(long_signal(), [candle(0, 103.2, 99.9)])
    assert result["closed"]["result"] == "TP1"
    assert result["closed"]["ambiguous"] is True
    assert result["closed"]["exit_price"] == 101.5


def test_sl_inside_live_tp1_activation_candle_is_conservative_minus_one_r():
    signal = long_signal()
    activation_at = T0 + timedelta(seconds=30)
    signal["outcome"].update({
        "state": STATE_TP1_HIT,
        "tp1_at": activation_at,
        "protection_armed_at": activation_at,
        "protective_stop_price": 101.5,
        "last_live_price_at": activation_at,
    })
    result = run(signal, [candle(0, 101.6, 98.8)])
    assert result["closed"]["result"] == "SL"
    assert result["closed"]["r_real"] == -1.0
    assert result["closed"]["ambiguous"] is True
    assert result["closed"]["via"] == "SL_TP1_ACTIVATION_CANDLE_AMBIGUOUS"


def test_existing_tp1_hit_trade_migrates_protection_from_tp1_to_entry():
    signal = long_signal()
    signal["outcome"].update({
        "state": STATE_TP1_HIT,
        "tp1_at": T0,
        "protection_armed_at": T0,
        "protective_stop_price": 101.5,  # previous release protected at TP1
        "tracked_until": T0,
    })
    result = advance_trade(signal, [], now=T0 + timedelta(minutes=1))
    assert result["outcome"]["protective_stop_price"] == 100.1
    assert result["advanced"] is True
    assert result["closed"] is None


def test_existing_tp1_hit_single_target_signal_is_settled_as_tp1():
    signal = long_signal(tps=(101.5,))
    signal["outcome"].update({
        "state": STATE_TP1_HIT,
        "tp1_at": T0,
        "protection_armed_at": T0,
        "protective_stop_price": 101.5,
    })
    result = advance_trade(signal, [], now=T0 + timedelta(minutes=1))
    assert result["closed"]["result"] == "TP1"
    assert result["closed"]["r_real"] == round(result["levels"]["r_tp1"], 4)
    assert result["outcome"]["state"] == STATE_CLOSED
