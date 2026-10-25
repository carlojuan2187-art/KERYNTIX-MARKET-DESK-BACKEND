import unittest

from engine import flow

T0 = 1_800_000_000.0  # "ahora" fijo


def make_candles(n, buy_fn, price_fn=lambda i: 100.0 + i * 0.01, vol=1000.0, vol_fn=None, end_open=True):
    """n velas de 5m terminando en T0 (la última, en curso si end_open con 2 min de edad)."""
    candles = []
    for i in range(n):
        start = T0 - (n - i) * 300 + (120 if end_open else 300)
        # la última vela abre hace 120 s si está en curso
        if end_open:
            start = T0 - 120 - (n - 1 - i) * 300
        v = vol_fn(i) if vol_fn else vol
        price = price_fn(i)
        buy = buy_fn(i)
        candles.append({
            "t": start, "o": price, "h": price * 1.001, "l": price * 0.999, "c": price_fn(i + 1) if i < n - 1 else price,
            "v": v, "close_t": start + 300, "qv": v * price, "tb": v * buy, "tbq": v * buy * price,
        })
    return candles


class ParseTests(unittest.TestCase):
    def test_parse_candles_skips_broken_rows(self):
        rows = [
            [1_000_000, "1", "2", "0.5", "1.5", "10", 1_299_999, "15", 5, "6", "9", "0"],
            [1, "x", "2", "0.5", "1.5", "10", 2, "15", 5, "6", "9", "0"],
            [1, 2],
            None,
        ]
        candles = flow.parse_candles(rows)
        self.assertEqual(len(candles), 1)
        self.assertEqual(candles[0]["tb"], 6.0)
        self.assertEqual(candles[0]["tbq"], 9.0)

    def test_normalize_symbol(self):
        self.assertEqual(flow.normalize_symbol("sol"), "SOLUSDT")
        self.assertEqual(flow.normalize_symbol("$avax/usdt"), "AVAXUSDT")
        self.assertEqual(flow.normalize_symbol("1000pepe"), "1000PEPEUSDT")
        for bad in ("hola mundo", "", "/", "a"):
            with self.assertRaises(ValueError):
                flow.normalize_symbol(bad)


class WindowTests(unittest.TestCase):
    def test_buy_pct_and_delta(self):
        candles = make_candles(60, lambda i: 0.7)
        w = flow.window_stats(candles, 60, T0)
        self.assertAlmostEqual(w["buy_pct"], 70.0)
        # delta = compra - venta = 0.7v - 0.3v = 0.4v por vela; 12 velas de 1000
        self.assertAlmostEqual(w["delta_base"], 0.4 * 1000 * 12)
        self.assertGreater(w["delta_quote"], 0)

    def test_open_candle_with_less_than_a_minute_is_excluded(self):
        candles = make_candles(60, lambda i: 0.5)
        candles[-1]["t"], candles[-1]["close_t"] = T0 - 10, T0 + 290   # abrió hace 10 s
        candles[-1]["tb"] = candles[-1]["v"]                            # 100 % compra, pero se ignora
        w = flow.window_stats(candles, 15, T0)
        self.assertAlmostEqual(w["buy_pct"], 50.0)

    def test_volume_ratio_uses_rate_so_partial_candle_does_not_distort(self):
        # volumen normal 1000 por vela; la vela en curso lleva 2 min con 400 (= ritmo normal)
        candles = make_candles(60, lambda i: 0.5, vol_fn=lambda i: 400.0 if i == 59 else 1000.0)
        w = flow.window_stats(candles, 15, T0)
        self.assertAlmostEqual(w["volume_ratio"], 1.0, delta=0.1)

    def test_volume_spike_detected(self):
        candles = make_candles(60, lambda i: 0.5, vol_fn=lambda i: 4000.0 if i >= 57 else 1000.0)
        w = flow.window_stats(candles, 15, T0)
        self.assertGreater(w["volume_ratio"], 3.0)

    def test_not_enough_candles(self):
        self.assertIsNone(flow.window_stats(make_candles(3, lambda i: 0.5), 60, T0))

    def test_cvd_series_points(self):
        candles = make_candles(60, lambda i: 0.8)
        series = flow.cvd_series(candles)
        self.assertEqual(len(series), 12)
        self.assertTrue(all(b > a for a, b in zip(series, series[1:])))  # sólo compra: sube siempre


class InterpretTests(unittest.TestCase):
    def data(self, buy15, buy1h, buy4h, p15=0.0, p1h=0.0, p4h=0.0, vol_ratio=None):
        mk = lambda label, minutes, buy, chg: {"label": label, "minutes": minutes, "buy_pct": buy, "price_chg": chg,
                                               "delta_base": (buy - 50) * 10, "delta_quote": (buy - 50) * 1000,
                                               "volume_ratio": vol_ratio if label == "15m" else None}
        return {"symbol": "BTCUSDT", "cvd": [1, 2, 3],
                "windows": [mk("15m", 15, buy15, p15), mk("1h", 60, buy1h, p1h), mk("4h", 240, buy4h, p4h)]}

    def test_price_up_sellers_dominate_is_a_warning(self):
        notes = flow.interpret_flow(self.data(58, 46, 44, p1h=0.4))
        self.assertTrue(notes[0].startswith("⚠ El precio sube pero los vendedores agresivos dominan"))
        self.assertIn("En 15m entra compra; todavía no cambia el 1h.", notes)

    def test_price_down_buyers_absorb(self):
        notes = flow.interpret_flow(self.data(55, 56, 54, p1h=-0.6))
        self.assertTrue(notes[0].startswith("ℹ El precio cae pero los compradores"))
        self.assertIn("Compra agresiva sostenida en los tres plazos.", notes)

    def test_backed_rally(self):
        self.assertTrue(flow.interpret_flow(self.data(60, 58, 55, p1h=0.8))[0].startswith("✅"))

    def test_balanced(self):
        notes = flow.interpret_flow(self.data(50, 50, 50))
        self.assertIn("equilibrado", notes[0])

    def test_strong_imbalance_and_volume(self):
        notes = flow.interpret_flow(self.data(70, 55, 50, vol_ratio=3.2))
        self.assertTrue(any("Desequilibrio fuerte en 15m" in n for n in notes))
        self.assertTrue(any("x3.2" in n for n in notes))


class FormatTests(unittest.TestCase):
    def test_full_message(self):
        candles = make_candles(60, lambda i: 0.58 if i > 50 else 0.45, price_fn=lambda i: 65000 + i * 5)
        text = flow.format_flow_message(flow.compute_flow("BTCUSDT", candles, T0), now=T0)
        self.assertIn("🌊 FLUJO · BTCUSDT", text)
        for label in ("15m", "1h", "4h"):
            self.assertIn(label, text)
        self.assertIn("compra", text)
        self.assertIn("BTC", text)
        self.assertIn("CVD 4h", text)
        self.assertIn("🔎 Lectura", text)
        self.assertIn("Informativo", text)
        self.assertLess(len(text), 4000)

    def test_helpers(self):
        self.assertEqual(flow.fmt_signed_big(-2100), "−2.10K")
        self.assertEqual(flow.fmt_signed_big(312), "+312")
        self.assertEqual(flow.fmt_usd(21_000_000), "+$21.0M")
        self.assertEqual(flow.fmt_pct(-0.46), "−0.5%")
        self.assertEqual(flow.bar(58), "▰▰▰▰▰▰▱▱▱▱")
        self.assertEqual(flow.sparkline([1, 1, 1]), "▄▄▄")
        self.assertEqual(flow.sparkline([1, 2, 3, 4])[0], "▁")
        self.assertEqual(flow.sparkline([1, 2, 3, 4])[-1], "█")


class CacheTests(unittest.TestCase):
    def setUp(self):
        flow.clear_cache()

    def test_cache_and_errors_not_cached(self):
        calls = []

        def get_json(path, params):
            calls.append(params["symbol"])
            if len(calls) == 1:
                raise RuntimeError("boom")
            return [[int((T0 - (60 - i) * 300) * 1000), "1", "1", "1", "1", "10", int((T0 - (59 - i) * 300) * 1000),
                     "10", 1, "5", "5", "0"] for i in range(60)]

        with self.assertRaises(RuntimeError):
            flow.get_candles("BTCUSDT", now=T0, get_json=get_json)
        flow.get_candles("BTCUSDT", now=T0, get_json=get_json)
        flow.get_candles("BTCUSDT", now=T0 + 5, get_json=get_json)   # caché
        self.assertEqual(len(calls), 2)
        flow.get_candles("BTCUSDT", now=T0 + 100, get_json=get_json)  # vencida
        self.assertEqual(len(calls), 3)

    def test_too_few_candles_raises(self):
        with self.assertRaises(flow.FlowUnavailable):
            flow.fetch_candles("BTCUSDT", lambda p, q: [])

    def test_invalid_symbol(self):
        with self.assertRaises(ValueError):
            flow.get_candles("btc")


if __name__ == "__main__":
    unittest.main()
