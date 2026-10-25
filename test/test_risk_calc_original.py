import unittest

from engine.risk_calc import (
    RiskInputError,
    calculate_position,
    format_risk_plan,
    parse_numbers,
)


class ParseNumbersTests(unittest.TestCase):
    def test_accepts_comma_and_dot_decimals(self):
        self.assertEqual(parse_numbers("1000 1,5"), [1000.0, 1.5])
        self.assertEqual(parse_numbers("$1000, riesgo 1.5%"), [1000.0, 1.5])

    def test_empty_or_text_only(self):
        self.assertEqual(parse_numbers(""), [])
        self.assertEqual(parse_numbers("hola"), [])


class CalculatePositionTests(unittest.TestCase):
    def test_long_sizing_and_rr(self):
        plan = calculate_position(1000, 1, 67437.75, 67180.0, [67890.0, 68310.0], direction="LONG")
        self.assertEqual(plan["direction"], "LONG")
        self.assertAlmostEqual(plan["risk_usdt"], 10.0)
        # perder exactamente el riesgo si toca el stop
        self.assertAlmostEqual(plan["qty"] * (67437.75 - 67180.0), 10.0, places=6)
        self.assertAlmostEqual(plan["targets"][0]["rr"], 1.8, places=1)
        self.assertAlmostEqual(plan["targets"][0]["profit"], 10.0 * plan["targets"][0]["rr"], places=6)

    def test_short_direction_inferred(self):
        plan = calculate_position(500, 2, 100.0, 102.0, [97.0])
        self.assertEqual(plan["direction"], "SHORT")
        self.assertAlmostEqual(plan["qty"], 5.0)  # 10 USDT / 2 de distancia
        self.assertAlmostEqual(plan["targets"][0]["profit"], 15.0)

    def test_minimum_leverage_covers_notional(self):
        # stop 0.5% con riesgo 1% => posición = 2x el capital
        plan = calculate_position(1000, 1, 100.0, 99.5)
        self.assertAlmostEqual(plan["notional"], 2000.0)
        self.assertEqual(plan["min_leverage"], 2)
        self.assertTrue(plan["feasible"])

    def test_tight_stop_is_not_feasible(self):
        # stop 0.05% con riesgo 5% => posición 100x el capital
        plan = calculate_position(1000, 5, 100.0, 99.95)
        self.assertGreater(plan["min_leverage"], plan["max_leverage"])
        self.assertFalse(plan["feasible"])
        self.assertIn("⛔", format_risk_plan(plan))

    def test_max_leverage_is_capped(self):
        plan = calculate_position(1000, 1, 100.0, 99.0)
        self.assertLessEqual(plan["max_leverage"], 25)

    def test_fees_are_round_trip_on_notional(self):
        plan = calculate_position(1000, 1, 100.0, 99.0)
        self.assertAlmostEqual(plan["fees"], plan["notional"] * 0.0005 * 2)

    def test_invalid_inputs(self):
        for args in (
            (0, 1, 100, 99),          # capital
            (1000, 0, 100, 99),       # riesgo muy bajo
            (1000, 50, 100, 99),      # riesgo muy alto
            (1000, 1, 100, 100),      # stop = entrada
            (1000, 1, -5, 99),        # precio negativo
        ):
            with self.assertRaises(RiskInputError, msg=str(args)):
                calculate_position(*args)

    def test_direction_conflicts_with_stop(self):
        with self.assertRaises(RiskInputError):
            calculate_position(1000, 1, 100, 99, direction="SHORT")

    def test_tp_on_wrong_side(self):
        with self.assertRaises(RiskInputError):
            calculate_position(1000, 1, 100, 99, [98.0])  # LONG con TP debajo
        with self.assertRaises(RiskInputError):
            calculate_position(1000, 1, 100, 101, [102.0])  # SHORT con TP encima


class FormatTests(unittest.TestCase):
    def test_message_contents(self):
        plan = calculate_position(1000, 1, 67437.75, 67180.0, [67890.0, 68310.0], direction="LONG")
        text = format_risk_plan(plan, "BTCUSDT")
        for expected in ("CALCULADORA DE RIESGO", "BTCUSDT", "LONG", "TAMAÑO DE POSICIÓN",
                         "BTC", "TP1", "TP2", "En Stop Loss", "No es asesoría"):
            self.assertIn(expected, text)

    def test_manual_message_without_symbol(self):
        text = format_risk_plan(calculate_position(1000, 1, 100, 99, [102]))
        self.assertIn("unid.", text)

    def test_high_risk_warning(self):
        text = format_risk_plan(calculate_position(1000, 5, 100, 95))
        self.assertIn("Riesgo alto", text)
        self.assertNotIn("Riesgo alto", format_risk_plan(calculate_position(1000, 1, 100, 95)))

    def test_no_scientific_notation_for_tiny_prices(self):
        text = format_risk_plan(calculate_position(1000, 1, 0.00001234, 0.00001200), "PEPEUSDT")
        self.assertNotIn("e-", text)
        self.assertNotIn("e+", text)


if __name__ == "__main__":
    unittest.main()
