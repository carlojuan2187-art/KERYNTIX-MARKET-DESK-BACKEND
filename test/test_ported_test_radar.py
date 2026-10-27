import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

import pandas as pd

from engine import radar
from engine.regime import REGIME_DOWN, REGIME_UP
from test.test_ported_test_regime import make_frame


def frames(price=100.0):
    df_1h = make_frame("up", step_minutes=60)
    df_15m = make_frame("range", step_minutes=15)
    df_5m = make_frame("range", step_minutes=5)
    df_5m.loc[df_5m.index[-1], "close"] = price
    return df_1h, df_15m, df_5m


def fake_zone_factory(long_offset_atr=0.5, short_offset_atr=0.4):
    """Devuelve una zona de liquidez a una distancia conocida (en ATR) del precio."""
    def fake(df, direction, profile):
        atr = float(df.iloc[-1]["atr"])
        if direction == "LONG":
            return {"price": 100.0 - long_offset_atr * atr, "count": 3}
        return {"price": 100.0 + short_offset_atr * atr, "count": 1}
    return fake


class AnalyzeSymbolTests(unittest.TestCase):
    def test_watch_distance_math(self):
        df_1h, df_15m, df_5m = frames()
        with mock.patch("engine.radar._select_liquidity_zone", side_effect=fake_zone_factory()):
            item = radar.analyze_symbol("TESTUSDT", df_1h, df_15m, df_5m)
        self.assertIsNotNone(item)
        by_dir = {w["direction"]: w for w in item["watch"]}
        self.assertEqual(set(by_dir), {"LONG", "SHORT"})
        self.assertAlmostEqual(by_dir["LONG"]["distance_atr"], 0.5, places=1)
        self.assertAlmostEqual(by_dir["SHORT"]["distance_atr"], 0.4, places=1)
        self.assertEqual(by_dir["LONG"]["zone_count"], 3)
        self.assertEqual(by_dir["LONG"]["alignment"], "favor")   # 1H alcista
        self.assertEqual(by_dir["SHORT"]["alignment"], "contra")
        self.assertGreater(by_dir["LONG"]["watch_score"], by_dir["SHORT"]["watch_score"])

    def test_far_or_crossed_levels_are_ignored(self):
        df_1h, df_15m, df_5m = frames()
        with mock.patch("engine.radar._select_liquidity_zone", side_effect=fake_zone_factory(5.0, -1.0)):
            item = radar.analyze_symbol("TESTUSDT", df_1h, df_15m, df_5m)
        self.assertEqual(item["watch"], [])  # LONG muy lejos, SHORT ya cruzado

    def test_insufficient_history(self):
        df_1h, _, df_5m = frames()
        self.assertIsNone(radar.analyze_symbol("X", df_1h, make_frame("range", bars=20, step_minutes=15), df_5m))

    def test_unusual_movement(self):
        df_1h, df_15m, df_5m = frames(price=100.0)
        df_5m["close"] = 100.0
        df_5m.loc[df_5m.index[-13:], "close"] = 100.0
        df_5m.loc[df_5m.index[-13], "close"] = 94.0          # +6.4 % en 1h
        df_5m.loc[df_5m.index[-12:], "volume"] = 5000.0       # ~5x el promedio (≈1000)
        item = radar.analyze_symbol("TESTUSDT", df_1h, df_15m, df_5m)
        self.assertGreater(item["move"]["change_1h_pct"], 6.0)
        self.assertGreater(item["move"]["vol_ratio"], 4.0)


class SnapshotTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 10, 23, 15, 0, tzinfo=timezone.utc)

    def item(self, symbol, price, move=None, watch=()):
        return {"symbol": symbol, "price": price, "atr_pct": 0.6, "rel_volume": 1.4,
                "trend_1h": "UP", "watch": list(watch), "move": move}

    def obs(self, symbol, direction, minutes_ago, level, extreme):
        event = (self.now - timedelta(minutes=minutes_ago)).replace(tzinfo=None)  # naive UTC como en Mongo
        return {"symbol": symbol, "direction": direction, "event_at": event, "level": level,
                "extreme_price": extreme, "wick_ratio": 0.6, "sweep_distance_atr": 0.4}

    def test_sweeps_dedupe_expire_and_invalidate(self):
        items = [self.item("AAAUSDT", 101.0), self.item("BBBUSDT", 99.0), self.item("CCCUSDT", 50.0)]
        sweeps = [
            self.obs("AAAUSDT", "LONG", 10, 100.0, 99.0),
            self.obs("AAAUSDT", "LONG", 3, 100.0, 99.5),    # más reciente: gana
            self.obs("BBBUSDT", "LONG", 4, 100.0, 99.5),    # precio 99 < extremo 99.5: invalidado
            self.obs("CCCUSDT", "SHORT", 40, 50.0, 51.0),   # vencido (>20 min)
        ]
        snap = radar.build_radar_snapshot(items, sweeps, {"AAAUSDT": 0.0005}, now=self.now)
        self.assertEqual([s["symbol"] for s in snap["sweeps"]], ["AAAUSDT"])
        self.assertAlmostEqual(snap["sweeps"][0]["extreme_price"], 99.5)
        self.assertEqual(snap["scanned"], 3)
        self.assertEqual(snap["funding"]["AAAUSDT"], 0.0005)

    def test_movers_threshold(self):
        items = [
            self.item("UPUSDT", 10, move={"change_1h_pct": 6.2, "vol_ratio": 4.8}),
            self.item("DNUSDT", 10, move={"change_1h_pct": -4.1, "vol_ratio": 3.3}),
            self.item("QUIETUSDT", 10, move={"change_1h_pct": 1.0, "vol_ratio": 4.0}),
            self.item("THINUSDT", 10, move={"change_1h_pct": 9.0, "vol_ratio": 1.0}),
        ]
        snap = radar.build_radar_snapshot(items, [], now=self.now)
        self.assertEqual([m["symbol"] for m in snap["movers"]], ["UPUSDT", "DNUSDT"])


class FormatTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 10, 23, 15, 0, tzinfo=timezone.utc)
        self.regime = {"regime": REGIME_UP, "confidence": 78}
        self.watch = [{
            "symbol": "AVAXUSDT", "direction": "LONG", "level": 31.2, "distance_pct": 0.4, "distance_atr": 0.6,
            "zone_count": 3, "rel_volume": 1.4, "trend_1h": "UP", "alignment": "favor", "watch_score": 71,
        }]
        event = (self.now - timedelta(minutes=4)).replace(tzinfo=None)
        self.snapshot = radar.build_radar_snapshot(
            [{"symbol": "ETHUSDT", "price": 2480.0, "atr_pct": 0.5, "rel_volume": 1.4, "trend_1h": "UP",
              "watch": self.watch, "move": {"change_1h_pct": 4.2, "vol_ratio": 3.1}},
             {"symbol": "SOLUSDT", "price": 148.2, "atr_pct": 0.6, "rel_volume": 2.1, "trend_1h": "UP",
              "watch": [], "move": None}],
            [{"symbol": "ETHUSDT", "direction": "SHORT", "event_at": event, "level": 2486.0,
              "extreme_price": 2491.0, "wick_ratio": 0.64, "sweep_distance_atr": 0.35}],
            {"SOLUSDT": 0.0004}, now=self.now)
        self.setup = {
            "symbol": "SOLUSDT", "direction": "LONG", "status": "PENDING", "score": 82.0,
            "entry_price": 147.75, "entry_zone": {"low": 147.6, "high": 147.9}, "stop_loss": 146.0,
            "take_profits": [150.4, 152.9], "atr_value": 0.9,
            "liquidity_zone": 146.8, "liquidity_sweep_extreme_price": 146.35,
            "entry_expires_at": (self.now + timedelta(minutes=11)).replace(tzinfo=None),
            "components": [
                {"name": "liquidity_zone", "points": 18, "value": 3},
                {"name": "minimum_sweep", "points": 18, "value": 0.4},
                {"name": "recovery_close", "points": 16, "value": 0.7},
                {"name": "confirmation_volume", "points": 0, "value": 2.1},
                {"name": "close_position", "points": 12, "value": 0.8},
                {"name": "ema_reclaim", "points": 10, "value": 148},
                {"name": "barrier_room_rr", "points": 12, "value": 1.6},
            ],
        }

    def test_full_message(self):
        text = radar.format_radar_message(self.snapshot, [self.setup], self.regime, now=self.now)
        for expected in (
            "RADAR DE OPORTUNIDADES", "Régimen: 📈 alcista (78%)", "1 setup activo", "1 barrido sin confirmar",
            "🥇 SOLUSDT · LONG · Score 82/100", "Entrada 147.6–147.9", "TP1 150.4 (1.5R)", "TP2 152.9 (2.9R)",
            "Expira en 11 min", "Funding +0.040%: longs cargados", "liquidez 18/18",
            "🥈 ETHUSDT · SHORT · Score radar", "Contra el régimen alcista", "Falta: cierre 5M bajo 2,486",
            "En vigilancia", "AVAXUSDT LONG", "Movimientos inusuales", "ETHUSDT +4.2% en 1h",
        ):
            self.assertIn(expected, text)
        self.assertLessEqual(len(text), 4096)

    def test_empty_state(self):
        empty = radar.build_radar_snapshot([], [], now=self.now)
        text = radar.format_radar_message(empty, [], None, now=self.now)
        self.assertIn("Sin setups ni barridos activos", text)
        self.assertNotIn("Régimen:", text)

    def test_sweep_already_confirmed_is_not_duplicated(self):
        eth_setup = dict(self.setup, symbol="ETHUSDT", direction="SHORT")
        text = radar.format_radar_message(self.snapshot, [eth_setup], self.regime, now=self.now)
        self.assertNotIn("ETHUSDT · SHORT · Score radar", text)
        self.assertIn("0 barridos sin confirmar", text)

    def test_long_message_is_trimmed(self):
        many = [dict(self.setup, symbol=f"S{i}USDT") for i in range(5)]
        snap = dict(self.snapshot, watch=self.watch * 15, watch_total=15)
        text = radar.format_radar_message(snap, many, self.regime, now=self.now)
        self.assertLessEqual(len(text), radar.MAX_TELEGRAM_CHARS)

    def test_alignment_and_sweep_score(self):
        self.assertEqual(radar.alignment_for("LONG", {"regime": REGIME_UP})["status"], "favor")
        self.assertEqual(radar.alignment_for("LONG", {"regime": REGIME_DOWN})["status"], "contra")
        self.assertEqual(radar.alignment_for("LONG", None, "UP")["status"], "favor")
        self.assertEqual(radar.alignment_for("LONG", None, None)["status"], "neutral")
        sweep = {"sweep_distance_atr": 0.4, "wick_ratio": 0.6, "event_ts": self.now.timestamp(), "rel_volume": 1.5}
        fresh = radar.sweep_score(sweep, "favor", self.now.timestamp())
        old = radar.sweep_score(sweep, "favor", self.now.timestamp() + 15 * 60)
        against = radar.sweep_score(sweep, "contra", self.now.timestamp())
        self.assertGreater(fresh, old)
        self.assertGreater(fresh, against)
        self.assertLessEqual(fresh, 100)


if __name__ == "__main__":
    unittest.main()
