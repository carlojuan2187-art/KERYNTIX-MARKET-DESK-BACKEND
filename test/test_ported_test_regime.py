import unittest

import numpy as np
import pandas as pd

from engine.regime import (
    REGIME_DOWN, REGIME_RANGE, REGIME_UP,
    analyze_timeframe, compute_market_regime, format_regime_message,
    summarize_breadth, symbol_trend_state, trading_session,
)
from datetime import datetime, timezone


def make_frame(kind: str, bars: int = 220, seed: int = 7, step_minutes: int = 60) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    if kind == "up":
        close = 100 + np.cumsum(rng.normal(0.35, 0.25, bars))
    elif kind == "down":
        close = 300 - np.cumsum(rng.normal(0.35, 0.25, bars))
    else:  # lateral: ruido con reversión a la media alrededor de 100
        x = np.zeros(bars)
        for i in range(1, bars):
            x[i] = x[i - 1] * 0.85 + rng.normal(0, 0.6)
        close = 100 + x
    open_ = np.concatenate([[close[0]], close[:-1]])
    high = np.maximum(open_, close) + rng.uniform(0.05, 0.3, bars)
    low = np.minimum(open_, close) - rng.uniform(0.05, 0.3, bars)
    volume = rng.uniform(900, 1100, bars)
    end = pd.Timestamp.now(tz="UTC").floor("h") - pd.Timedelta(hours=1)
    close_time = pd.date_range(end=end, periods=bars, freq=f"{step_minutes}min")
    return pd.DataFrame({
        "open": open_, "high": high, "low": low, "close": close,
        "volume": volume, "close_time": close_time,
    })


def frames_for(kind):
    return {tf: make_frame(kind, step_minutes=m) for tf, m in (("15m", 15), ("1h", 60), ("4h", 240))}


class RegimeTests(unittest.TestCase):
    def test_uptrend_detected(self):
        snap = compute_market_regime(frames_for("up"))
        self.assertEqual(snap["regime"], REGIME_UP)
        self.assertGreater(snap["confidence"], 50)

    def test_downtrend_detected(self):
        snap = compute_market_regime(frames_for("down"))
        self.assertEqual(snap["regime"], REGIME_DOWN)

    def test_range_detected(self):
        # Un rango real no es perfecto: exigimos que la mayoría de series
        # con reversión a la media se clasifiquen como RANGE.
        hits = sum(
            compute_market_regime({"1h": make_frame("range", seed=seed)})["regime"] == REGIME_RANGE
            for seed in range(20)
        )
        self.assertGreaterEqual(hits, 14)

    def test_insufficient_history_returns_none(self):
        self.assertIsNone(analyze_timeframe(make_frame("up", bars=30)))
        self.assertIsNone(compute_market_regime({"1h": make_frame("up", bars=30)}))

    def test_requires_1h(self):
        self.assertIsNone(compute_market_regime({"15m": make_frame("up")}))

    def test_open_candle_is_ignored(self):
        df = make_frame("up")
        df.loc[df.index[-1], "close_time"] = pd.Timestamp.now(tz="UTC") + pd.Timedelta(minutes=30)
        full = analyze_timeframe(make_frame("up"))
        trimmed = analyze_timeframe(df)
        self.assertIsNotNone(trimmed)
        self.assertNotEqual(full["close"], trimmed["close"])

    def test_breadth(self):
        states = [symbol_trend_state(make_frame("up", seed=i)) for i in range(8)]
        states += [symbol_trend_state(make_frame("down", seed=i)) for i in range(4)]
        states.append(None)
        breadth = summarize_breadth(states)
        self.assertEqual(breadth["total"], 12)
        self.assertAlmostEqual(breadth["bullish_pct"], 67, delta=1)
        self.assertIsNone(summarize_breadth(states[:5]))

    def test_message_renders_with_and_without_optionals(self):
        breadth = summarize_breadth([symbol_trend_state(make_frame("up", seed=i)) for i in range(12)])
        full = compute_market_regime(frames_for("up"), breadth=breadth, funding_rate=0.0007, oi_change_24h_pct=3.2)
        text = format_regime_message(full)
        for expected in ("RÉGIMEN DEL MERCADO", "Confianza", "Amplitud", "Funding", "Qué significa para el bot"):
            self.assertIn(expected, text)
        minimal = format_regime_message(compute_market_regime(frames_for("range")))
        self.assertNotIn("Amplitud", minimal)
        self.assertNotIn("Derivados", minimal)
        self.assertLess(len(minimal), 4096)

    def test_session(self):
        self.assertEqual(trading_session(datetime(2026, 1, 1, 14, tzinfo=timezone.utc))["name"], "Londres + Nueva York")
        self.assertEqual(trading_session(datetime(2026, 1, 1, 3, tzinfo=timezone.utc))["name"], "Asia")


if __name__ == "__main__":
    unittest.main()
