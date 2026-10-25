import importlib.util
import re
import unittest

from engine import derivatives as dv

HAS_TELEGRAM = importlib.util.find_spec("telegram") is not None
if HAS_TELEGRAM:
    from app.menus import main_menu, signal_detail_menu, derivatives_home_menu


def fake_api(funding="0.0001", oi=(100.0, 105.0, 110.0), ls=("1.20", "1.80"), top="1.10", fail=()):
    """get_json falso. `oi` y `ls` van de la muestra más antigua a la más reciente."""
    def get_json(path, params):
        for name in fail:
            if name in path:
                raise RuntimeError("boom")
        if path.endswith("premiumIndex"):
            return {"lastFundingRate": funding, "nextFundingTime": 4_000_000_000_000,
                    "markPrice": "100.2", "indexPrice": "100.0"}
        if path.endswith("openInterestHist"):
            return [{"sumOpenInterestValue": str(v)} for v in oi]
        if path.endswith("globalLongShortAccountRatio"):
            return [{"longShortRatio": r, "longAccount": "0.64", "shortAccount": "0.36"} for r in ls]
        if path.endswith("topLongShortPositionRatio"):
            return [{"longShortRatio": top}]
        raise AssertionError(path)
    return get_json


class FetchTests(unittest.TestCase):
    def test_parses_all_blocks(self):
        data = dv.fetch_derivatives("BTCUSDT", fake_api())
        self.assertAlmostEqual(data["funding"], 0.0001)
        self.assertAlmostEqual(data["premium_pct"], 0.2, places=3)
        self.assertEqual(data["oi_value"], 110.0)
        self.assertAlmostEqual(data["oi_change_1h"], (110 / 105 - 1) * 100)
        self.assertAlmostEqual(data["oi_change_24h"], 10.0)
        self.assertEqual(data["ls_ratio"], 1.8)
        self.assertEqual(data["ls_ratio_24h"], 1.2)
        self.assertAlmostEqual(data["ls_long_pct"], 64.0)
        self.assertEqual(data["top_ratio"], 1.10)

    def test_partial_failure_still_returns_rest(self):
        data = dv.fetch_derivatives("ETHUSDT", fake_api(fail=("openInterestHist", "topLong")))
        self.assertIsNone(data["oi_value"])
        self.assertIsNone(data["top_ratio"])
        self.assertIsNotNone(data["funding"])
        text = dv.format_derivatives_message(data)
        self.assertIn("Open interest: n/d", text)

    def test_all_failing_raises(self):
        with self.assertRaises(dv.DerivativesUnavailable):
            dv.fetch_derivatives("BTCUSDT", fake_api(fail=("premium", "openInterest", "global", "topLong")))

    def test_symbol_validation(self):
        for bad in ("btc", "BTCUSD", "BTC/USDT", "", None, "A" * 30 + "USDT"):
            with self.assertRaises(ValueError, msg=str(bad)):
                dv.fetch_derivatives(bad, fake_api())
        self.assertEqual(dv.validate_symbol("1000pepeusdt"), "1000PEPEUSDT")


class CacheTests(unittest.TestCase):
    def setUp(self):
        dv.clear_cache()
        self.calls = 0
        self._orig = dv.fetch_derivatives

        def counting(symbol, get_json=None):
            self.calls += 1
            return self._orig(symbol, fake_api())
        dv.fetch_derivatives = counting

    def tearDown(self):
        dv.fetch_derivatives = self._orig
        dv.clear_cache()

    def test_cache_hits_within_ttl_and_expires(self):
        dv.get_derivatives("BTCUSDT", now=1000.0)
        dv.get_derivatives("BTCUSDT", now=1000.0 + dv.CACHE_SECONDS - 1)
        self.assertEqual(self.calls, 1)
        dv.get_derivatives("BTCUSDT", now=1000.0 + dv.CACHE_SECONDS + 1)
        self.assertEqual(self.calls, 2)

    def test_errors_are_not_cached(self):
        def failing(symbol, get_json=None):
            self.calls += 1
            raise dv.DerivativesUnavailable("x")
        dv.fetch_derivatives = failing
        for _ in range(2):
            with self.assertRaises(dv.DerivativesUnavailable):
                dv.get_derivatives("BTCUSDT", now=1.0)
        self.assertEqual(self.calls, 2)


class InterpretTests(unittest.TestCase):
    def base(self, **kw):
        data = {"funding": 0.0001, "ls_ratio": 1.0, "oi_change_24h": 0.0}
        data.update(kw)
        return data

    def test_neutral_market(self):
        notes = dv.interpret(self.base())
        self.assertIn("Funding neutral.", notes)
        self.assertTrue(any("equilibradas" in n for n in notes))

    def test_crowded_longs_warn_a_long_signal(self):
        notes = dv.interpret(self.base(funding=0.0006, ls=2.2), "LONG")
        self.assertTrue(any(n.startswith("⚠️ Tu LONG va con la mayoría") for n in notes))

    def test_crowded_shorts_are_contrarian_for_a_long(self):
        notes = dv.interpret(self.base(funding=-0.0004, ls=0.5), "LONG")
        self.assertTrue(any(n.startswith("ℹ️ Tu LONG va contra la mayoría") for n in notes))

    def test_short_signal_mirrors(self):
        notes = dv.interpret(self.base(funding=0.0006, ls=2.2), "SHORT")
        self.assertTrue(any("Tu SHORT va contra la mayoría" in n for n in notes))
        notes = dv.interpret(self.base(funding=-0.0006, ls=0.4), "SHORT")
        self.assertTrue(any(n.startswith("⚠️ Tu SHORT va con la mayoría") for n in notes))

    def test_no_bias_message_and_oi(self):
        notes = dv.interpret(self.base(oi_change_24h=7.5), "LONG")
        self.assertTrue(any("OI en alza" in n for n in notes))
        self.assertIn("Sin sesgo claro de posicionamiento para tu LONG.", notes)
        self.assertTrue(any("OI a la baja" in n for n in dv.interpret(self.base(oi_change_24h=-6))))

    def test_missing_metrics_do_not_crash(self):
        self.assertEqual(dv.interpret({}), [])
        self.assertEqual(len(dv.interpret({}, "LONG")), 1)


class FormatTests(unittest.TestCase):
    def test_message_contents_and_countdown(self):
        data = dv.fetch_derivatives("BTCUSDT", fake_api())
        now = 4_000_000_000 - 2 * 3600 - 15 * 60
        text = dv.format_derivatives_message(data, "LONG", now=now)
        for expected in ("DERIVADOS · BTCUSDT · LONG", "Funding: +0.0100%", "longs pagan a shorts",
                         "en 2h 15m", "Open interest: 110 USDT", "24h +10.0%",
                         "Cuentas long/short: 1.80 (64% long / 36% short)", "hace 24h: 1.20",
                         "Top traders", "🔎 Lectura", "no modifica ni filtra señales"):
            self.assertIn(expected, text)
        self.assertLess(len(text.splitlines()), 22)

    def test_helpers(self):
        self.assertEqual(dv.fmt_big(8.42e9), "8.42B")
        self.assertEqual(dv.fmt_big(3.1e6), "3.10M")
        self.assertEqual(dv.fmt_big(None), "n/d")
        self.assertEqual(dv.fmt_countdown(None), "n/d")
        self.assertEqual(dv.fmt_countdown(1_000_000, now=999.99), "en breve")
        self.assertEqual(dv.fmt_countdown(1_000_000 + 45 * 60_000, now=1000), "en 45m")


@unittest.skipUnless(HAS_TELEGRAM, "python-telegram-bot no instalado")
class MenuWiringTests(unittest.TestCase):
    def cbs(self, markup):
        return [b.callback_data for row in markup.inline_keyboard for b in row]

    def test_buttons_present_and_within_limit(self):
        self.assertIn("derivatives", self.cbs(main_menu()))
        doc_id = "c" * 24
        self.assertIn(f"dvs_{doc_id}", self.cbs(signal_detail_menu(doc_id)))
        self.assertFalse(any(c.startswith("dvs_") for c in self.cbs(signal_detail_menu())))
        for cb in self.cbs(derivatives_home_menu(["BTCUSDT", "1000PEPEUSDT"])):
            self.assertLessEqual(len(cb.encode()), 64)

    def test_patterns(self):
        self.assertTrue(re.match("^dv_[A-Z0-9]{2,20}USDT$", "dv_1000PEPEUSDT"))
        self.assertFalse(re.match("^dv_[A-Z0-9]{2,20}USDT$", "dv_btcusdt"))
        self.assertTrue(re.match("^dvs_[0-9a-f]{24}$", "dvs_" + "ab" * 12))


if __name__ == "__main__":
    unittest.main()
