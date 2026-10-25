from __future__ import annotations

import pytest

from engine.watchlist import compute_score, normalize_symbol, short_name, user_symbols
from engine.regime import REGIME_DOWN, REGIME_RANGE, REGIME_UP


def test_normalize_symbols_matches_bot_conventions():
    assert normalize_symbol("avax") == "AVAXUSDT"
    assert normalize_symbol("AVAX/USDT") == "AVAXUSDT"
    assert normalize_symbol("$1000pepe") == "1000PEPEUSDT"
    assert short_name("BTCUSDT") == "BTC"
    with pytest.raises(ValueError):
        normalize_symbol("BTC USDT")
    # Matches the bot's historical normalizer: it appends USDT to unqualified bases;
    # market-backed endpoints then reject symbols absent from the actual universe.
    assert normalize_symbol("BTCUSDC") == "BTCUSDCUSDT"


def test_user_symbols_discards_bad_entries_dedupes_and_caps_at_ten():
    user = {"watchlist": ["BTCUSDT", "BAD SYMBOL", "BTCUSDT"] + [f"COIN{i}USDT" for i in range(20)]}
    rows = user_symbols(user)
    assert rows[0] == "BTCUSDT"
    assert len(rows) == 10
    assert len(rows) == len(set(rows))


def test_score_strong_bullish_and_sweep_bonus():
    analyses = {
        "15m": {"regime": REGIME_UP, "adx": 40},
        "1h": {"regime": REGIME_UP, "adx": 40},
        "4h": {"regime": REGIME_UP, "adx": 40},
    }
    without = compute_score(analyses)
    with_sweep = compute_score(analyses, {"direction": "LONG", "minutes": 0})
    assert without["direction"] == "LONG" and without["score"] == 100
    assert with_sweep["score"] == 100
    assert with_sweep["signs"] == {"15m": 1, "1h": 1, "4h": 1}


def test_score_directions_cancel_and_missing_timeframes_are_ignored():
    mixed = {
        "15m": {"regime": REGIME_UP, "adx": 40},
        "1h": {"regime": REGIME_DOWN, "adx": 40},
        "4h": {"regime": REGIME_RANGE, "adx": 10},
    }
    score = compute_score(mixed)
    assert score is not None
    assert score["lean"] == "SHORT"  # the 1H weight is higher than 15M
    assert score["score"] < 55
    assert score["direction"] == "NEUTRAL"
    assert compute_score({"15m": None, "1h": None, "4h": None}) is None
