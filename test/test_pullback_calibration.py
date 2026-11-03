import pytest

from engine.liquidity_strategy import (
    PULLBACK_ATR_MIN,
    PULLBACK_RETRACE_FRACTION,
    _build_pullback_entry,
)


def test_pullback_calibration_uses_slightly_deeper_reference_entry():
    assert PULLBACK_RETRACE_FRACTION == pytest.approx(0.42)
    assert PULLBACK_ATR_MIN == pytest.approx(0.24)


def test_long_entry_waits_for_a_slightly_deeper_pullback():
    entry = _build_pullback_entry(
        direction="LONG", level=100.0, confirmation_close=101.0,
        market_price=105.0, atr_now=1.0,
    )
    # Desired retracement = 1.0 * 0.42 = 0.42 ATR; entry = 105 - 0.42.
    assert entry == pytest.approx(104.58)


def test_short_entry_waits_for_a_slightly_deeper_pullback():
    entry = _build_pullback_entry(
        direction="SHORT", level=110.0, confirmation_close=109.0,
        market_price=108.0, atr_now=1.0,
    )
    # Mirrored geometry: entry = 108 + 0.42.
    assert entry == pytest.approx(108.42)
