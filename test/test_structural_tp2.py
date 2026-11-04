import pandas as pd
import pytest

import engine.liquidity_strategy as strategy


def test_tp2_can_use_extended_real_swing_history_without_moving_tp1(monkeypatch):
    monkeypatch.setattr(strategy, "TARGET_LOOKBACK_15M", 10)
    monkeypatch.setattr(strategy, "TARGET_LOOKBACK_1H", 12)
    monkeypatch.setattr(strategy, "TARGET_TP2_LOOKBACK_MULTIPLIER", 2.0)
    monkeypatch.setattr(strategy, "TARGET_PIVOT_BARS", 1)
    monkeypatch.setattr(strategy, "TARGET_FRONTRUN_ATR", 0.0)
    monkeypatch.setattr(strategy, "TARGET_CLUSTER_ATR", 0.05)
    monkeypatch.setattr(strategy, "TARGET_MIN_GAP_ATR", 0.35)
    monkeypatch.setattr(strategy, "TARGET_MAX_ATR", 6.0)

    # A recent structural high provides TP1. An older, confirmed high just
    # outside the normal lookback is available only to the extended TP2 search.
    highs = [100.0] * 20
    highs[4] = 100.5
    highs[5] = 105.0
    highs[14] = 102.0
    highs[15] = 103.0
    highs[16] = 102.0
    lows = [99.0] * 20
    lows[5] = 98.0
    frame = pd.DataFrame({"high": highs, "low": lows, "close": [100.0] * 20})

    trade, reason = strategy._build_structural_trade(
        df_15m=frame,
        df_1h=None,
        direction="LONG",
        entry_price=100.0,
        atr_now=1.0,
        sweep_extreme_price=99.5,
    )

    assert reason is None
    assert trade is not None
    assert len(trade["take_profits"]) == 2
    assert trade["take_profits"][0] == pytest.approx(103.0)
    assert trade["take_profits"][1] > trade["take_profits"][0]
    assert "EXT" in trade["targets"][1]["source"]


def test_tp2_is_not_synthesized_when_no_second_structural_pool_exists(monkeypatch):
    monkeypatch.setattr(strategy, "TARGET_LOOKBACK_15M", 10)
    monkeypatch.setattr(strategy, "TARGET_LOOKBACK_1H", 12)
    monkeypatch.setattr(strategy, "TARGET_TP2_LOOKBACK_MULTIPLIER", 2.0)
    monkeypatch.setattr(strategy, "TARGET_PIVOT_BARS", 1)
    monkeypatch.setattr(strategy, "TARGET_FRONTRUN_ATR", 0.0)
    monkeypatch.setattr(strategy, "TARGET_CLUSTER_ATR", 0.25)
    monkeypatch.setattr(strategy, "TARGET_MIN_GAP_ATR", 0.35)
    monkeypatch.setattr(strategy, "TARGET_MAX_ATR", 6.0)
    highs = [100.0] * 20
    highs[14] = 103.0
    highs[15] = 102.0
    highs[16] = 101.0
    lows = [99.0] * 20
    frame = pd.DataFrame({"high": highs, "low": lows, "close": [100.0] * 20})

    trade, reason = strategy._build_structural_trade(
        df_15m=frame,
        df_1h=None,
        direction="LONG",
        entry_price=100.0,
        atr_now=1.0,
        sweep_extreme_price=99.5,
    )

    # The current range maximum can support TP1, but no separate structural pool exists.
    assert trade is not None
    assert len(trade["take_profits"]) == 1
